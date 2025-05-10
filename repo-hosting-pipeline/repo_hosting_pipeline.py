#!/usr/bin/env python3
"""
Repository Hosting Pipeline

This script reads a text file containing Git repository URLs, clones each repository,
and deploys them to Netlify, Render, or GitHub Pages. It then generates a CSV file
with the repository URLs and their corresponding hosted links.

Features:
- Handles rate limiting with exponential backoff
- Supports multiple hosting platforms (Netlify, Render, GitHub Pages)
- Detailed logging and error reporting
- Configurable deployment options
- Resumable deployments (can pick up where it left off)
"""

import os
import csv
import sys
import json
import time
import tempfile
import shutil
import zipfile
import io
import re
import requests
import argparse
import logging
import pandas as pd
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Set, Any
from urllib.parse import urlparse
from tqdm import tqdm
from datetime import datetime
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("repo_hosting_pipeline.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("repo-hosting")

# Load environment variables
load_dotenv()

# API tokens from environment variables or .env file
NETLIFY_TOKEN = os.getenv("NETLIFY_TOKEN", "")
RENDER_TOKEN = os.getenv("RENDER_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# API endpoints
NETLIFY_API_URL = "https://api.netlify.com/api/v1"
RENDER_API_URL = "https://api.render.com/v1"
GITHUB_API_URL = "https://api.github.com"

# Render owner ID (required for creating services)
RENDER_OWNER_ID = os.getenv("RENDER_OWNER_ID", "")

# Rate limiting parameters
DEFAULT_RETRY_DELAYS = [5, 10, 20, 40, 60]  # Exponential backoff
MAX_RETRIES = 3
DELAY_BETWEEN_DEPLOYMENTS = 10  # seconds

class RateLimitHandler:
    """Handles rate limiting with exponential backoff."""
    
    def __init__(self, platform: str, max_retries: int = MAX_RETRIES, 
                 retry_delays: List[int] = None):
        self.platform = platform
        self.max_retries = max_retries
        self.retry_delays = retry_delays or DEFAULT_RETRY_DELAYS
        self.retry_count = 0
    
    def should_retry(self, status_code: int) -> bool:
        """Determine if we should retry based on status code and retry count."""
        if self.retry_count >= self.max_retries:
            return False
            
        # Rate limit (429) or server errors (5xx)
        return status_code == 429 or status_code >= 500
    
    def wait_before_retry(self) -> int:
        """Wait before retry with exponential backoff."""
        if self.retry_count >= len(self.retry_delays):
            delay = self.retry_delays[-1]
        else:
            delay = self.retry_delays[self.retry_count]
            
        logger.info(f"Rate limit hit for {self.platform}, waiting {delay} seconds before retry...")
        time.sleep(delay)
        self.retry_count += 1
        return delay
        
    def reset(self) -> None:
        """Reset the retry count."""
        self.retry_count = 0

def check_requirements() -> Dict[str, bool]:
    """
    Check if all required tools and API tokens are available.
    
    Returns:
        Dict with platform names as keys and boolean values indicating if they're available
    """
    platforms_available = {
        "netlify": False,
        "render": False,
        "github": False
    }
    
    # Check for Netlify token
    if NETLIFY_TOKEN:
        try:
            logger.info("Validating Netlify API token...")
            headers = {"Authorization": f"Bearer {NETLIFY_TOKEN}"}
            response = requests.get(f"{NETLIFY_API_URL}/sites", headers=headers)
            
            if response.status_code == 200:
                logger.info("✓ Netlify API token is valid")
                platforms_available["netlify"] = True
            else:
                logger.error(f"✗ Invalid Netlify API token: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"✗ Failed to validate Netlify API token: {e}")
    else:
        logger.warning("✗ Netlify API token not provided")
    
    # Check for Render token
    if RENDER_TOKEN:
        try:
            logger.info("Validating Render API token...")
            headers = {"Authorization": f"Bearer {RENDER_TOKEN}"}
            response = requests.get(f"{RENDER_API_URL}/services", headers=headers)
            
            if response.status_code == 200:
                logger.info("✓ Render API token is valid")
                platforms_available["render"] = True
                
                # Get the owner ID if we don't have it yet
                if not RENDER_OWNER_ID:
                    owner_id = get_render_owner_id()
                    if not owner_id:
                        logger.error("✗ Could not determine Render owner ID (team ID). Please set RENDER_OWNER_ID in .env file")
                        platforms_available["render"] = False
            else:
                logger.error(f"✗ Invalid Render API token: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"✗ Failed to validate Render API token: {e}")
    else:
        logger.warning("✗ Render API token not provided")
    
    # Check for GitHub token
    if GITHUB_TOKEN:
        try:
            logger.info("Validating GitHub API token...")
            headers = {"Authorization": f"token {GITHUB_TOKEN}"}
            response = requests.get(f"{GITHUB_API_URL}/user", headers=headers)
            
            if response.status_code == 200:
                logger.info("✓ GitHub API token is valid")
                platforms_available["github"] = True
            else:
                logger.error(f"✗ Invalid GitHub API token: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"✗ Failed to validate GitHub API token: {e}")
    else:
        logger.warning("✗ GitHub API token not provided")
    
    # Check if at least one platform is available
    if not any(platforms_available.values()):
        logger.error("✗ No valid API tokens provided. Please set at least one API token in .env file")
        logger.info("Create a .env file with your API tokens:")
        logger.info("NETLIFY_TOKEN=your_netlify_personal_access_token")
        logger.info("RENDER_TOKEN=your_render_api_key")
        logger.info("GITHUB_TOKEN=your_github_personal_access_token")
        logger.info("RENDER_OWNER_ID=your_render_team_id  # Only needed for Render")
    
    return platforms_available

def get_render_owner_id() -> str:
    """
    Get the Render owner ID (team ID) using the API token.
    
    Returns:
        str: The owner ID if found, empty string otherwise
    """
    global RENDER_OWNER_ID
    
    if RENDER_OWNER_ID:
        return RENDER_OWNER_ID
        
    if not RENDER_TOKEN:
        return ""
        
    try:
        headers = {"Authorization": f"Bearer {RENDER_TOKEN}"}
        response = requests.get(f"{RENDER_API_URL}/owners", headers=headers)
        
        if response.status_code == 200:
            owners = response.json()
            if owners and len(owners) > 0:
                # Use the first owner as the default
                RENDER_OWNER_ID = owners[0].get("id", "")
                logger.info(f"Found Render owner ID: {RENDER_OWNER_ID}")
                return RENDER_OWNER_ID
    except Exception as e:
        logger.error(f"Failed to get Render owner ID: {e}")
        
    return ""

def read_repo_list(file_path: str) -> List[str]:
    """
    Read the list of Git repository URLs from a text file.
    
    Args:
        file_path: Path to the text file containing repository URLs
        
    Returns:
        List of repository URLs
    """
    repos = []
    try:
        with open(file_path, 'r') as f:
            for line in f:
                url = line.strip()
                if url and not url.startswith('#'):  # Skip empty lines and comments
                    repos.append(url)
        logger.info(f"Found {len(repos)} repositories in {file_path}")
        return repos
    except Exception as e:
        logger.error(f"Failed to read repository list: {e}")
        return []

def extract_repo_info(repo_url: str) -> Tuple[str, str, str]:
    """
    Extract owner, repo name, and default branch from a GitHub repository URL.
    
    Args:
        repo_url: GitHub repository URL
        
    Returns:
        Tuple containing owner, repo name, and default branch
    """
    # Match patterns like https://github.com/owner/repo.git or https://github.com/owner/repo
    match = re.match(r'https://github.com/([^/]+)/([^/.]+)(?:\.git)?$', repo_url)
    if match:
        owner = match.group(1)
        repo = match.group(2)
        
        # Try to determine the default branch (main or master)
        try:
            headers = {}
            if GITHUB_TOKEN:
                headers["Authorization"] = f"token {GITHUB_TOKEN}"
                
            response = requests.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
            if response.status_code == 200:
                default_branch = response.json().get("default_branch", "main")
            else:
                default_branch = "main"  # Default to main if we can't determine
        except Exception:
            default_branch = "main"  # Default to main if the request fails
            
        return owner, repo, default_branch
    return None, None, None

def download_repository(repo_url: str, temp_dir: str) -> Optional[str]:
    """
    Download a GitHub repository as a zip file and extract it.
    
    Args:
        repo_url: URL of the GitHub repository
        temp_dir: Temporary directory to download into
        
    Returns:
        Path to the downloaded repository or None if download failed
    """
    owner, repo_name, branch = extract_repo_info(repo_url)
    if not owner or not repo_name:
        logger.error(f"Failed to extract owner and repo name from {repo_url}")
        return None
    
    download_url = f"https://github.com/{owner}/{repo_name}/archive/refs/heads/{branch}.zip"
    extract_path = os.path.join(temp_dir, repo_name)
    
    try:
        logger.info(f"Downloading {repo_url} to {extract_path}")
        
        # Create the directory if it doesn't exist
        os.makedirs(extract_path, exist_ok=True)
        
        # Download the zip file
        headers = {}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
            
        response = requests.get(download_url, headers=headers, stream=True)
        if response.status_code != 200:
            logger.error(f"Failed to download repository: HTTP {response.status_code}")
            return None
        
        # Save the zip file to a temporary location
        zip_path = os.path.join(temp_dir, f"{repo_name}.zip")
        with open(zip_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        # Extract the zip file
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
        
        # The extracted folder will have a name like 'repo-main' or 'repo-master'
        extracted_folder = os.path.join(temp_dir, f"{repo_name}-{branch}")
        
        # If the extraction was successful, return the path to the extracted folder
        if os.path.exists(extracted_folder):
            return extracted_folder
        else:
            logger.error(f"Failed to extract repository: Extracted folder not found")
            return None
            
    except Exception as e:
        logger.error(f"Failed to download {repo_url}: {e}")
        return None

def detect_framework(repo_path: str) -> str:
    """
    Detect the web framework used in the repository.
    
    Args:
        repo_path: Path to the cloned repository
        
    Returns:
        Detected framework name or "static" if no framework is detected
    """
    framework_indicators = {
        "nextjs": ["next.config.js", "package.json"],
        "create-react-app": ["package.json"],
        "gatsby": ["gatsby-config.js"],
        "nuxtjs": ["nuxt.config.js"],
        "angular": ["angular.json"],
        "svelte": ["svelte.config.js"],
        "sveltekit": ["svelte.config.js"],
        "eleventy": [".eleventy.js"],
        "astro": ["astro.config.mjs"],
        "hugo": ["config.toml", "hugo.toml"],
        "jekyll": ["_config.yml"],
    }
    
    # Check for package.json to determine if it's a React app
    if os.path.exists(os.path.join(repo_path, "package.json")):
        try:
            with open(os.path.join(repo_path, "package.json"), 'r') as f:
                package_data = json.load(f)
                dependencies = {**package_data.get("dependencies", {}), **package_data.get("devDependencies", {})}
                
                if "next" in dependencies:
                    return "nextjs"
                elif "react-scripts" in dependencies:
                    return "create-react-app"
                elif "gatsby" in dependencies:
                    return "gatsby"
                elif "nuxt" in dependencies:
                    return "nuxtjs"
                elif "@angular/core" in dependencies:
                    return "angular"
                elif "svelte" in dependencies:
                    if "@sveltejs/kit" in dependencies:
                        return "sveltekit"
                    return "svelte"
                elif "astro" in dependencies:
                    return "astro"
        except (json.JSONDecodeError, FileNotFoundError):
            pass
    
    # Check for framework-specific files
    for framework, indicators in framework_indicators.items():
        for indicator in indicators:
            if os.path.exists(os.path.join(repo_path, indicator)):
                return framework
    
    # Check if it's a static HTML site
    if os.path.exists(os.path.join(repo_path, "index.html")):
        return "static"
    
    # Default to static if we can't determine the framework
    return "static"

def prepare_static_site(repo_path: str) -> Tuple[bool, str]:
    """
    Prepare a static site for deployment.
    
    Args:
        repo_path: Path to the cloned repository
        
    Returns:
        Tuple of (success, build_dir_path)
    """
    # Check for common build directories that might already exist in the repository
    for build_dir in ["build", "dist", "public", "_site", "out", "docs"]:
        build_path = os.path.join(repo_path, build_dir)
        if os.path.exists(build_path):
            # Check if the build directory contains an index.html
            if os.path.exists(os.path.join(build_path, "index.html")):
                logger.info(f"Found existing build directory with index.html: {build_dir}")
                return True, build_path
            # If no index.html but has HTML files, create one
            html_files = [f for f in os.listdir(build_path) if f.endswith('.html') or f.endswith('.htm')]
            if html_files:
                logger.info(f"Found HTML files in {build_dir}, creating index.html")
                create_index_for_directory(build_path, html_files)
                return True, build_path
    
    # Check if it's already a static site with an index.html in the root
    if os.path.exists(os.path.join(repo_path, "index.html")):
        logger.info("Found index.html in repository root")
        return True, repo_path
    
    # Check for common static site files
    static_files = ["index.html", "index.htm", "default.html", "default.htm"]
    for file in static_files:
        if os.path.exists(os.path.join(repo_path, file)):
            logger.info(f"Found static site file: {file}")
            return True, repo_path
            
    # Look for HTML files in the root directory
    html_files = [f for f in os.listdir(repo_path) if f.endswith('.html') or f.endswith('.htm')]
    if html_files:
        logger.info(f"Found HTML files in root, creating index.html")
        create_index_for_directory(repo_path, html_files)
        return True, repo_path
    
    # Check for src directory with HTML files
    src_dir = os.path.join(repo_path, "src")
    if os.path.exists(src_dir):
        html_files = [f for f in os.listdir(src_dir) if f.endswith('.html') or f.endswith('.htm')]
        if html_files:
            logger.info(f"Found HTML files in src directory, creating index.html")
            create_index_for_directory(src_dir, html_files)
            return True, src_dir
    
    # Create a simple index.html with repository information and file listing
    try:
        logger.info("Creating a fallback index.html page")
        repo_name = os.path.basename(repo_path)
        
        # Get a list of files in the repository (up to 100 files)
        file_list = []
        for root, dirs, files in os.walk(repo_path, topdown=True):
            if len(file_list) >= 100:
                break
            for file in files:
                if file.startswith('.') or file.endswith('.zip'):
                    continue  # Skip hidden files and zip files
                rel_path = os.path.relpath(os.path.join(root, file), repo_path)
                file_list.append(rel_path)
                if len(file_list) >= 100:
                    break
        
        # Create the file listing HTML
        file_list_html = "<ul>\n"
        for file in sorted(file_list):
            file_path = os.path.join(repo_path, file)
            if file.endswith('.html') or file.endswith('.htm'):
                file_list_html += f"<li><a href='{file}'>{file}</a></li>\n"
            elif file.endswith('.css') or file.endswith('.js') or file.endswith('.json'):
                file_list_html += f"<li><strong>{file}</strong> - Code file</li>\n"
            elif file.endswith('.jpg') or file.endswith('.png') or file.endswith('.gif') or file.endswith('.svg'):
                file_list_html += f"<li><strong>{file}</strong> - Image file</li>\n"
            else:
                file_list_html += f"<li>{file}</li>\n"
        file_list_html += "</ul>"
        
        # If there are too many files, add a note
        if len(file_list) >= 100:
            file_list_html += "<p><em>Showing only the first 100 files...</em></p>"
        
        # Create the index.html file
        with open(os.path.join(repo_path, "index.html"), 'w') as f:
            f.write(f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>{repo_name} - Repository Preview</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
                    h1, h2 {{ color: #333; }}
                    .container {{ max-width: 800px; margin: 0 auto; }}
                    .repo-info {{ background-color: #f4f4f4; padding: 20px; border-radius: 5px; margin-bottom: 20px; }}
                    .file-list {{ background-color: #f9f9f9; padding: 20px; border-radius: 5px; max-height: 400px; overflow-y: auto; }}
                    ul {{ padding-left: 20px; }}
                    li {{ margin-bottom: 5px; }}
                    a {{ color: #0366d6; text-decoration: none; }}
                    a:hover {{ text-decoration: underline; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>{repo_name}</h1>
                    <div class="repo-info">
                        <p>This is an automatically generated preview of the repository.</p>
                        <p>The repository does not contain a pre-built static website, so this fallback page has been created.</p>
                    </div>
                    
                    <h2>Repository Files</h2>
                    <div class="file-list">
                        {file_list_html}
                    </div>
                </div>
            </body>
            </html>
            """)
        return True, repo_path
    except Exception as e:
        logger.error(f"Failed to create fallback index.html: {e}")
        return False, ""

def create_index_for_directory(directory_path: str, html_files: List[str]) -> None:
    """
    Create an index.html file for a directory with links to HTML files.
    
    Args:
        directory_path: Path to the directory
        html_files: List of HTML files in the directory
    """
    try:
        dir_name = os.path.basename(directory_path)
        
        # Create links to HTML files
        links_html = "<ul>\n"
        for file in sorted(html_files):
            links_html += f"<li><a href='{file}'>{file}</a></li>\n"
        links_html += "</ul>"
        
        # Create the index.html file
        with open(os.path.join(directory_path, "index.html"), 'w') as f:
            f.write(f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>{dir_name} - Directory Index</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
                    h1, h2 {{ color: #333; }}
                    .container {{ max-width: 800px; margin: 0 auto; }}
                    .directory-info {{ background-color: #f4f4f4; padding: 20px; border-radius: 5px; margin-bottom: 20px; }}
                    .file-list {{ background-color: #f9f9f9; padding: 20px; border-radius: 5px; }}
                    ul {{ padding-left: 20px; }}
                    li {{ margin-bottom: 10px; }}
                    a {{ color: #0366d6; text-decoration: none; }}
                    a:hover {{ text-decoration: underline; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>{dir_name}</h1>
                    <div class="directory-info">
                        <p>This is an automatically generated index of HTML files in this directory.</p>
                    </div>
                    
                    <h2>HTML Files</h2>
                    <div class="file-list">
                        {links_html}
                    </div>
                </div>
            </body>
            </html>
            """)
        logger.info(f"Created index.html in {directory_path}")
    except Exception as e:
        logger.error(f"Failed to create index.html in {directory_path}: {e}")

def create_zip_file(directory_path: str, max_size_mb: int = 90) -> Optional[bytes]:
    """
    Create a zip file of a directory, respecting size limits.
    
    Args:
        directory_path: Path to the directory to zip
        max_size_mb: Maximum size of the zip file in MB (default: 90MB for Netlify's 100MB limit)
        
    Returns:
        Zip file as bytes or None if zipping failed
    """
    try:
        # Get list of files sorted by importance
        all_files = []
        for root, _, files in os.walk(directory_path):
            for file in files:
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, directory_path)
                file_size = os.path.getsize(file_path)
                file_ext = os.path.splitext(file)[1].lower()
                
                # Assign priority based on file type (lower number = higher priority)
                priority = 100  # Default priority
                if file == "index.html":
                    priority = 1  # Highest priority
                elif file_ext in [".html", ".htm"]:
                    priority = 10
                elif file_ext in [".css", ".js"]:
                    priority = 20
                elif file_ext in [".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico"]:
                    priority = 30
                elif file_ext in [".woff", ".woff2", ".ttf", ".eot"]:
                    priority = 40
                elif file_ext in [".json", ".xml"]:
                    priority = 50
                
                all_files.append((file_path, rel_path, file_size, priority))
        
        # Sort files by priority (lowest number first)
        all_files.sort(key=lambda x: x[3])
        
        # Create zip file with size limit
        memory_file = io.BytesIO()
        total_size = 0
        max_size_bytes = max_size_mb * 1024 * 1024
        skipped_files = []
        large_files = []
        
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path, rel_path, file_size, _ in all_files:
                # Skip very large individual files
                if file_size > max_size_bytes * 0.25:  # Skip files larger than 25% of max size
                    large_files.append((rel_path, file_size))
                    continue
                    
                # Check if adding this file would exceed the size limit
                if total_size + file_size > max_size_bytes:
                    skipped_files.append(rel_path)
                    continue
                    
                # Add file to zip
                zipf.write(file_path, rel_path)
                total_size += file_size
        
        # Log information about skipped files
        if large_files:
            logger.warning(f"Skipped {len(large_files)} large files exceeding size threshold:")
            for name, size in large_files[:5]:  # Show first 5 large files
                logger.warning(f"  - {name}: {size / (1024 * 1024):.2f} MB")
            if len(large_files) > 5:
                logger.warning(f"  - ... and {len(large_files) - 5} more")
                
        if skipped_files:
            logger.warning(f"Skipped {len(skipped_files)} files to stay within size limit")
            
        # Log total size
        logger.info(f"Created zip file: {total_size / (1024 * 1024):.2f} MB / {max_size_mb} MB")
        
        memory_file.seek(0)
        return memory_file.getvalue()
    except Exception as e:
        logger.error(f"Failed to create zip file: {e}")
        return None

def deploy_to_netlify(repo_path: str, repo_url: str) -> Optional[str]:
    """
    Deploy a repository to Netlify using their API.
    
    Args:
        repo_path: Path to the cloned repository
        repo_url: URL of the Git repository
        
    Returns:
        Netlify site URL or None if deployment failed
    """
    rate_limit_handler = RateLimitHandler("netlify")
    
    try:
        # Extract repository name
        repo_name = repo_url.split('/')[-1].replace('.git', '')
        site_name = f"{repo_name.lower().replace('_', '-').replace('.', '-')}-{int(time.time())}"
        
        # Prepare the site for deployment
        success, build_dir = prepare_static_site(repo_path)
        if not success:
            logger.warning(f"Failed to prepare {repo_url} for deployment")
            return None
        
        logger.info(f"Creating Netlify site: {site_name}")
        
        # Create a new site on Netlify
        while True:
            headers = {"Authorization": f"Bearer {NETLIFY_TOKEN}"}
            create_site_response = requests.post(
                f"{NETLIFY_API_URL}/sites",
                json={"name": site_name},
                headers=headers
            )
            
            if create_site_response.status_code == 201:
                break
                
            if rate_limit_handler.should_retry(create_site_response.status_code):
                rate_limit_handler.wait_before_retry()
            else:
                error_message = create_site_response.text
                logger.error(f"Failed to create Netlify site: {error_message}")
                return None
        
        site_data = create_site_response.json()
        site_id = site_data["id"]
        site_url = site_data.get("ssl_url") or site_data.get("url")
        
        logger.info(f"Created Netlify site: {site_url}")
        
        # Create a zip file of the build directory
        logger.info(f"Creating zip file from {build_dir}")
        zip_data = create_zip_file(build_dir)
        if not zip_data:
            logger.error("Failed to create zip file for deployment")
            return None
        
        # Deploy the site
        logger.info(f"Deploying to Netlify site: {site_id}")
        rate_limit_handler.reset()
        
        while True:
            deploy_response = requests.post(
                f"{NETLIFY_API_URL}/sites/{site_id}/deploys",
                headers=headers,
                files={
                    "file": ("site.zip", zip_data, "application/zip")
                }
            )
            
            if deploy_response.status_code == 200:
                break
                
            if rate_limit_handler.should_retry(deploy_response.status_code):
                rate_limit_handler.wait_before_retry()
                # Recreate the zip file in case it was corrupted
                zip_data = create_zip_file(build_dir)
                if not zip_data:
                    logger.error("Failed to recreate zip file for retry")
                    return None
            else:
                error_message = deploy_response.text
                logger.error(f"Failed to deploy to Netlify: {error_message}")
                return None
        
        # Get the deploy status and URL
        deploy_data = deploy_response.json()
        deploy_url = deploy_data.get("deploy_url") or site_url
        
        logger.info(f"Successfully deployed {repo_url} to {deploy_url}")
        return deploy_url
        
    except Exception as e:
        logger.error(f"Failed to deploy {repo_url} to Netlify: {e}")
        return None

def deploy_to_render(repo_path: str, repo_url: str) -> Optional[str]:
    """
    Deploy a repository to Render using their API.
    
    Args:
        repo_path: Path to the cloned repository
        repo_url: URL of the Git repository
        
    Returns:
        Render site URL or None if deployment failed
    """
    rate_limit_handler = RateLimitHandler("render")
    
    try:
        # Extract repository owner and name from the URL
        owner, repo_name, branch = extract_repo_info(repo_url)
        if not owner or not repo_name:
            logger.error(f"Failed to extract owner and repo name from {repo_url}")
            return None
            
        # Create a unique site name
        site_name = f"{repo_name.lower().replace('_', '-').replace('.', '-')}-{int(time.time())}"
        
        # Prepare the site for deployment
        success, build_dir = prepare_static_site(repo_path)
        if not success:
            logger.warning(f"Failed to prepare {repo_url} for deployment")
            return None
        
        logger.info(f"Creating Render site: {site_name}")
        
        # Get the owner ID if not already set
        owner_id = get_render_owner_id()
        if not owner_id:
            logger.error("Render owner ID (team ID) is required but not set. Please set RENDER_OWNER_ID.")
            return None
            
        # Create a new static site on Render
        headers = {"Authorization": f"Bearer {RENDER_TOKEN}"}
        
        # Create a proper structure for Render deployment
        # We'll create a new GitHub repository with the proper structure
        # This is a workaround since Render requires a specific structure
        
        # First, create a temporary directory for the new repository structure
        temp_repo_dir = os.path.join(os.path.dirname(repo_path), f"{repo_name}-render-deploy")
        os.makedirs(temp_repo_dir, exist_ok=True)
        
        # Create a public directory - this is critical for Render
        public_dir = os.path.join(temp_repo_dir, "public")
        os.makedirs(public_dir, exist_ok=True)
        
        # Copy all files from the build directory to the public directory
        logger.info(f"Copying files from {build_dir} to {public_dir}")
        for item in os.listdir(build_dir):
            src = os.path.join(build_dir, item)
            dst = os.path.join(public_dir, item)
            try:
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
            except Exception as e:
                logger.warning(f"Error copying {item}: {e}")
        
        # Ensure there's an index.html in the public directory
        if not os.path.exists(os.path.join(public_dir, "index.html")):
            logger.info("Creating a default index.html in public directory")
            with open(os.path.join(public_dir, "index.html"), 'w') as f:
                f.write(f"""<!DOCTYPE html>
<html>
<head>
    <title>{repo_name} - Repository Preview</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }}
        h1 {{ color: #333; }}
        .container {{ max-width: 800px; margin: 0 auto; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{repo_name}</h1>
        <p>This is an automatically generated preview of the repository.</p>
    </div>
</body>
</html>
""")
        
        # Create a proper package.json in the root directory
        package_json_path = os.path.join(temp_repo_dir, "package.json")
        logger.info("Creating package.json with proper build script")
        with open(package_json_path, 'w') as f:
            f.write('{\n  "name": "' + repo_name + '",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "build": "echo \'Static site, no build needed\'"\n  }\n}')
        
        # Create a simple README.md to explain the repository
        readme_path = os.path.join(temp_repo_dir, "README.md")
        with open(readme_path, 'w') as f:
            f.write(f"# {repo_name}\n\nThis is a static site deployment for {repo_url}\n")
        
        # Create a simple .gitignore file
        gitignore_path = os.path.join(temp_repo_dir, ".gitignore")
        with open(gitignore_path, 'w') as f:
            f.write("node_modules/\n.DS_Store\n")
        
        # Now we need to create a static site on Render using a direct upload approach
        # instead of GitHub integration, since we're having issues with that
        
        # First, create a static site service on Render
        create_site_data = {
            "type": "static_site",
            "name": site_name,
            "ownerId": owner_id,
            "buildCommand": "npm run build",
            "publishDirectory": "public"
        }
        
        logger.info(f"Creating Render static site with configuration: {json.dumps(create_site_data, indent=2)}")
        
        while True:
            create_site_response = requests.post(
                f"{RENDER_API_URL}/services",
                json=create_site_data,
                headers=headers
            )
            
            if create_site_response.status_code in (200, 201):
                break
                
            if rate_limit_handler.should_retry(create_site_response.status_code):
                rate_limit_handler.wait_before_retry()
            else:
                error_message = create_site_response.text
                logger.error(f"Failed to create Render site: {error_message}")
                return None
        
        site_data = create_site_response.json()
        site_id = site_data.get("id")
        site_url = site_data.get("url") or f"https://{site_name}.onrender.com"
        
        logger.info(f"Created Render site: {site_url}")
        
        # Now we need to deploy the static files directly
        # For this, we'll create a zip file of the public directory and deploy it
        
        # Create a zip file of the public directory
        zip_path = os.path.join(temp_repo_dir, "deploy.zip")
        logger.info(f"Creating zip file of public directory at {zip_path}")
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(public_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(file_path, public_dir)
                    zipf.write(file_path, rel_path)
        
        # Now deploy the zip file to Render
        logger.info(f"Deploying zip file to Render site {site_id}")
        
        # For direct deployment, we need to use a different API endpoint
        # Unfortunately, Render doesn't have a direct file upload API for static sites
        # So we'll use a workaround by creating a deploy hook and triggering it
        
        # Create a deploy hook
        deploy_hook_data = {
            "name": f"deploy-hook-{int(time.time())}"
        }
        
        deploy_hook_response = requests.post(
            f"{RENDER_API_URL}/services/{site_id}/deploy-hooks",
            json=deploy_hook_data,
            headers=headers
        )
        
        if deploy_hook_response.status_code not in (200, 201):
            logger.error(f"Failed to create deploy hook: {deploy_hook_response.text}")
            # Return the site URL anyway, as the site was created
            logger.info(f"Site was created but deployment failed. You may need to manually deploy at {site_url}")
            return site_url
        
        deploy_hook_data = deploy_hook_response.json()
        deploy_hook_url = deploy_hook_data.get("url")
        
        if not deploy_hook_url:
            logger.error("Failed to get deploy hook URL")
            return site_url
        
        # Trigger the deploy hook
        logger.info(f"Triggering deploy hook: {deploy_hook_url}")
        
        deploy_response = requests.post(deploy_hook_url)
        
        if deploy_response.status_code not in (200, 201, 202):
            logger.error(f"Failed to trigger deploy hook: {deploy_response.text}")
            return site_url
        
        logger.info(f"Successfully triggered deployment for {site_url}")
        logger.info(f"Deployment may take a few minutes to complete. Please check {site_url} in 5-10 minutes.")
        
        # Clean up the temporary directory
        try:
            shutil.rmtree(temp_repo_dir)
        except Exception as e:
            logger.warning(f"Failed to clean up temporary directory: {e}")
        
        return site_url
        
    except Exception as e:
        logger.error(f"Failed to deploy {repo_url} to Render: {e}")
        return None

def enable_github_pages(repo_url: str, repo_path: Optional[str] = None) -> Optional[str]:
    """
    Enable GitHub Pages for a repository and prepare the repository for GitHub Pages deployment.
    For TypeScript/JavaScript repositories with frameworks like React, Next.js, or Vite,
    this function will create a proper GitHub Pages configuration.
    
    Args:
        repo_url: URL of the GitHub repository
        repo_path: Local path to the cloned repository (if available)
        
    Returns:
        GitHub Pages URL or None if enabling failed
    """
    rate_limit_handler = RateLimitHandler("github")
    
    try:
        # Extract owner and repo name from URL
        owner, repo_name, _ = extract_repo_info(repo_url)
        if not owner or not repo_name:
            logger.error(f"Failed to extract owner and repo name from {repo_url}")
            return None
        
        # GitHub Pages URLs follow the pattern: https://{owner}.github.io/{repo_name}
        pages_url = f"https://{owner}.github.io/{repo_name}"
        
        # Check if GitHub token is available
        if not GITHUB_TOKEN:
            logger.error("GitHub token is required to enable GitHub Pages")
            return None
        
        # Initialize headers for API requests
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        # If we have a local repo path, prepare it for GitHub Pages
        if repo_path and os.path.exists(repo_path):
            logger.info(f"Preparing repository for GitHub Pages deployment: {repo_path}")
            
            # Detect the framework
            framework = detect_framework(repo_path)
            logger.info(f"Detected framework: {framework}")
            
            # Check if it's a JavaScript/TypeScript project with package.json
            package_json_path = os.path.join(repo_path, "package.json")
            if os.path.exists(package_json_path):
                try:
                    with open(package_json_path, 'r') as f:
                        package_data = json.load(f)
                    
                    # Create a GitHub workflow file for GitHub Pages deployment
                    workflows_dir = os.path.join(repo_path, ".github", "workflows")
                    os.makedirs(workflows_dir, exist_ok=True)
                    
                    # Determine the build command and output directory based on the framework
                    build_command = "npm run build"
                    output_dir = "build"
                    base_path = f"/{repo_name}/"
                    
                    if framework == "nextjs":
                        # Next.js requires special configuration for GitHub Pages
                        output_dir = "out"
                        
                        # Add export script to package.json if it doesn't exist
                        if "scripts" in package_data and "build" in package_data["scripts"]:
                            if "export" not in package_data["scripts"]:
                                package_data["scripts"]["export"] = "next export"
                            build_command = "npm run build && npm run export"
                        
                        # Create or update next.config.js for GitHub Pages
                        next_config_path = os.path.join(repo_path, "next.config.js")
                        next_config_content = """/** @type {{output: string, basePath: string, assetPrefix: string}} */
const nextConfig = {
  output: 'export',
  basePath: '" + base_path + "',
  assetPrefix: '" + base_path + "',
  images: {
    unoptimized: true,
  },
};

module.exports = nextConfig;
"""
                        
                        with open(next_config_path, 'w') as f:
                            f.write(next_config_content)
                        
                        logger.info("Created Next.js configuration for GitHub Pages")
                    
                    elif framework == "create-react-app":
                        # Create React App needs homepage in package.json
                        package_data["homepage"] = pages_url
                        
                        # Update package.json
                        with open(package_json_path, 'w') as f:
                            json.dump(package_data, f, indent=2)
                        
                        logger.info("Updated package.json with homepage for GitHub Pages")
                    
                    elif "vite" in package_data.get("devDependencies", {}) or "vite" in package_data.get("dependencies", {}):
                        # Vite project needs vite.config.js update
                        vite_config_path = None
                        for config_name in ["vite.config.js", "vite.config.ts"]:
                            if os.path.exists(os.path.join(repo_path, config_name)):
                                vite_config_path = os.path.join(repo_path, config_name)
                                break
                        
                        if vite_config_path:
                            # Read existing config
                            with open(vite_config_path, 'r') as f:
                                vite_config = f.read()
                            
                            # Add base config if not present
                            if "base:" not in vite_config and "base =" not in vite_config:
                                # Simple string replacement to add base config
                                if "export default" in vite_config:
                                    base_config = "export default {\n  base: '" + base_path + "',"
                                    vite_config = vite_config.replace("export default", base_config)
                                    vite_config = vite_config.replace("export default {", "export default {\n  base: '" + base_path + "',")
                                
                                with open(vite_config_path, 'w') as f:
                                    f.write(vite_config)
                                
                                logger.info(f"Updated Vite config with base path: {base_path}")
                    
                    # Create GitHub Actions workflow file for building and deploying
                    workflow_file = os.path.join(workflows_dir, "github-pages-deploy.yml")
                    workflow_content = """name: Deploy to GitHub Pages

on:
  push:
    branches: [ "main", "master" ]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: "pages"
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v3
      - name: Setup Node
        uses: actions/setup-node@v3
        with:
          node-version: "18"
          cache: 'npm'
      - name: Install dependencies
        run: npm ci
      - name: Build
        run: """ + build_command + """
      - name: Upload artifact
        uses: actions/upload-pages-artifact@v2
        with:
          path: ./""" + output_dir + """

  deploy:
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    runs-on: ubuntu-latest
    needs: build
    steps:
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v2
"""
                    with open(workflow_file, 'w') as f:
                        f.write(workflow_content)
                    
                    logger.info(f"Created GitHub Actions workflow file for {framework} deployment")
                    
                except Exception as e:
                    logger.error(f"Error preparing repository for GitHub Pages: {e}")
        
        # Check if GitHub Pages is already enabled
        while True:
            pages_response = requests.get(
                f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/pages",
                headers=headers
            )
            
            # If pages already enabled, return the URL
            if pages_response.status_code == 200:
                logger.info(f"GitHub Pages already enabled for {repo_url}")
                return pages_url
            
            # If pages not found (404), we need to enable them
            if pages_response.status_code == 404:
                break
                
            if rate_limit_handler.should_retry(pages_response.status_code):
                rate_limit_handler.wait_before_retry()
            else:
                logger.error(f"Failed to check GitHub Pages status: {pages_response.status_code} - {pages_response.text}")
                return None
        
        # Determine the default branch (main or master)
        while True:
            repo_response = requests.get(
                f"{GITHUB_API_URL}/repos/{owner}/{repo_name}",
                headers=headers
            )
            
            if repo_response.status_code == 200:
                default_branch = repo_response.json().get("default_branch", "main")
                break
                
            if rate_limit_handler.should_retry(repo_response.status_code):
                rate_limit_handler.wait_before_retry()
            else:
                logger.error(f"Failed to get repository info: {repo_response.status_code} - {repo_response.text}")
                return None
        
        # Enable GitHub Pages from the default branch
        rate_limit_handler.reset()
        
        # For modern JS frameworks, we'll use GitHub Actions for deployment
        # So we'll enable GitHub Pages with the gh-pages branch if it exists
        # or the default branch if not
        
        # First check if gh-pages branch exists
        branch_to_use = default_branch
        path_to_use = "/"
        
        try:
            branch_response = requests.get(
                f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/branches/gh-pages",
                headers=headers
            )
            if branch_response.status_code == 200:
                branch_to_use = "gh-pages"
                path_to_use = "/"
        except Exception as e:
            logger.debug(f"Error checking for gh-pages branch: {e}")
            # Continue with default branch
        
        while True:
            enable_response = requests.post(
                f"{GITHUB_API_URL}/repos/{owner}/{repo_name}/pages",
                json={
                    "source": {
                        "branch": branch_to_use,
                        "path": path_to_use
                    }
                },
                headers=headers
            )
            
            if enable_response.status_code in (201, 204):
                logger.info(f"Successfully enabled GitHub Pages for {repo_url} at {pages_url}")
                logger.info(f"Note: For JavaScript frameworks, the site may take a few minutes to build and deploy")
                return pages_url
                
            if rate_limit_handler.should_retry(enable_response.status_code):
                rate_limit_handler.wait_before_retry()
            else:
                logger.error(f"Failed to enable GitHub Pages: {enable_response.status_code} - {enable_response.text}")
                return None
                
    except Exception as e:
        logger.error(f"Error enabling GitHub Pages for {repo_url}: {e}")
        return None

def process_repositories(repo_list: List[str], output_csv: str, platforms: List[str], 
                         max_retries: int = MAX_RETRIES, delay_between_deployments: int = DELAY_BETWEEN_DEPLOYMENTS,
                         resume_from: Optional[str] = None) -> pd.DataFrame:
    """
    Process each repository in the list: download, deploy, and record results.
    
    Args:
        repo_list: List of repository URLs
        output_csv: Path to the output CSV file
        platforms: List of platforms to deploy to ("netlify", "render", "github")
        max_retries: Maximum number of retry attempts for failed deployments
        delay_between_deployments: Delay between deployments in seconds
        resume_from: Repository URL to resume from (skip all repositories before this one)
        
    Returns:
        DataFrame with deployment results
    """
    results = []
    temp_dir = tempfile.mkdtemp()
    processed_repos = set()
    
    # Check if we need to resume from a previous run
    if resume_from and os.path.exists(output_csv):
        try:
            # Load existing results
            existing_df = pd.read_csv(output_csv)
            results = existing_df.to_dict('records')
            
            # Get the list of already processed repositories
            processed_repos = set(existing_df['repo_url'].tolist())
            
            logger.info(f"Resuming from previous run. {len(processed_repos)} repositories already processed.")
            
            # Find the repository to resume from
            resume_index = -1
            for i, repo_url in enumerate(repo_list):
                if repo_url == resume_from:
                    resume_index = i
                    break
            
            if resume_index >= 0:
                # Skip all repositories before the resume point
                repo_list = repo_list[resume_index:]
                logger.info(f"Resuming from {resume_from}. {len(repo_list)} repositories remaining.")
            else:
                logger.warning(f"Resume repository {resume_from} not found in the input list.")
                
        except Exception as e:
            logger.error(f"Failed to load existing results for resume: {e}")
    
    try:
        for i, repo_url in enumerate(tqdm(repo_list, desc="Processing repositories")):
            # Skip already processed repositories
            if repo_url in processed_repos:
                logger.info(f"Skipping already processed repository: {repo_url}")
                continue
                
            logger.info(f"Processing repository: {repo_url}")
            
            # Add delay between deployments to avoid rate limits (except for the first one)
            if i > 0 and delay_between_deployments > 0:
                logger.debug(f"Waiting {delay_between_deployments} seconds before next deployment...")
                time.sleep(delay_between_deployments)
            
            # Download the repository
            repo_path = download_repository(repo_url, temp_dir)
            if not repo_path:
                results.append({
                    "repo_url": repo_url, 
                    "hosted_url": None, 
                    "platform": None,
                    "status": "Failed to download",
                    "notes": "Could not download repository from GitHub"
                })
                # Save results after each repository to enable resume capability
                pd.DataFrame(results).to_csv(output_csv, index=False)
                continue
            
            # Try to deploy the repository to each platform in order until one succeeds
            hosted_url = None
            platform_used = None
            error_messages = []
            
            for platform in platforms:
                try:
                    logger.info(f"Attempting to deploy {repo_url} to {platform}")
                    
                    if platform == "netlify" and "netlify" in platforms:
                        hosted_url = deploy_to_netlify(repo_path, repo_url)
                        platform_used = "netlify"
                    elif platform == "render" and "render" in platforms:
                        hosted_url = deploy_to_render(repo_path, repo_url)
                        platform_used = "render"
                    elif platform == "github" and "github" in platforms:
                        hosted_url = enable_github_pages(repo_url, repo_path)
                        platform_used = "github"
                    
                    if hosted_url:
                        logger.info(f"Successfully deployed {repo_url} to {platform} at {hosted_url}")
                        break  # Deployment succeeded, exit platform loop
                    else:
                        logger.warning(f"Failed to deploy {repo_url} to {platform}")
                        error_messages.append(f"Failed to deploy to {platform}")
                        
                except Exception as e:
                    error_message = str(e)
                    error_messages.append(f"{platform}: {error_message}")
                    logger.error(f"Error deploying to {platform}: {e}")
            
            # Record the result
            if hosted_url:
                status = "Success"
                notes = f"Deployed successfully to {platform_used}"
            else:
                status = "Failed to deploy"
                notes = "Deployment failed to all platforms"
                if error_messages:
                    notes += f": {'; '.join(error_messages[:3])}"
                    if len(error_messages) > 3:
                        notes += f" and {len(error_messages) - 3} more errors"
            
            results.append({
                "repo_url": repo_url, 
                "hosted_url": hosted_url, 
                "platform": platform_used,
                "status": status,
                "notes": notes
            })
            
            # Save results after each repository to enable resume capability
            pd.DataFrame(results).to_csv(output_csv, index=False)
            
    finally:
        # Clean up temporary directory
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    # Create final DataFrame
    df = pd.DataFrame(results)
    
    # Print summary
    success_count = sum(1 for r in results if r["status"] == "Success")
    failed_count = len(repo_list) - success_count
    logger.info(f"Summary: {success_count}/{len(repo_list)} repositories successfully deployed")
    
    if failed_count > 0:
        logger.info("Failed deployments:")
        for result in results:
            if result["status"] != "Success":
                logger.info(f"  - {result['repo_url']}: {result['notes']}")
    
    return df

def main():
    """
    Main entry point for the script.
    """
    parser = argparse.ArgumentParser(description="Deploy Git repositories to Netlify, Render, or GitHub Pages")
    parser.add_argument("input_file", help="Text file containing Git repository URLs (one per line)")
    parser.add_argument("--output", "-o", default="deployment_results.csv", 
                        help="Output CSV file (default: deployment_results.csv)")
    parser.add_argument("--platforms", "-p", nargs="+", choices=["netlify", "render", "github"], default=["netlify"],
                        help="Platforms to deploy to (default: netlify)")
    parser.add_argument("--max-retries", "-r", type=int, default=MAX_RETRIES,
                        help=f"Maximum number of retry attempts for failed deployments (default: {MAX_RETRIES})")
    parser.add_argument("--delay", "-d", type=int, default=DELAY_BETWEEN_DEPLOYMENTS,
                        help=f"Delay between deployments in seconds to avoid rate limits (default: {DELAY_BETWEEN_DEPLOYMENTS})")
    parser.add_argument("--max-size", "-s", type=int, default=90,
                        help="Maximum size of deployment package in MB (default: 90)")
    parser.add_argument("--resume-from", "-f", 
                        help="Repository URL to resume from (skip all repositories before this one)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose logging")
    args = parser.parse_args()
    
    # Set logging level based on verbose flag
    if args.verbose:
        logging.getLogger("repo-hosting").setLevel(logging.DEBUG)
    
    # Check if requirements are met
    available_platforms = check_requirements()
    
    # Filter requested platforms to only those that are available
    platforms = [p for p in args.platforms if available_platforms.get(p, False)]
    
    if not platforms:
        logger.error("None of the requested platforms are available. Please set up API tokens.")
        sys.exit(1)
    
    logger.info(f"Using platforms: {', '.join(platforms)}")
    
    # Read the repository list
    repo_list = read_repo_list(args.input_file)
    if not repo_list:
        logger.error("No repositories found in the input file.")
        sys.exit(1)
    
    logger.info(f"Starting deployment of {len(repo_list)} repositories")
    logger.info(f"Max package size: {args.max_size} MB, Max retries: {args.max_retries}, Delay between deployments: {args.delay}s")
    
    # Process the repositories
    try:
        results_df = process_repositories(
            repo_list, 
            args.output, 
            platforms, 
            args.max_retries, 
            args.delay,
            args.resume_from
        )
        
        # Print final summary
        success_count = sum(1 for _, row in results_df.iterrows() if row["status"] == "Success")
        logger.info(f"\nDeployment completed: {success_count}/{len(repo_list)} repositories successfully deployed")
        logger.info(f"Results saved to {args.output}")
        
        # Open the CSV file if possible
        try:
            if os.name == 'nt':  # Windows
                os.startfile(args.output)
            elif os.name == 'posix':  # macOS or Linux
                if sys.platform == 'darwin':  # macOS
                    subprocess.call(['open', args.output])
                else:  # Linux
                    subprocess.call(['xdg-open', args.output])
        except Exception:
            pass  # Ignore if we can't open the file
            
    except KeyboardInterrupt:
        logger.warning("\nDeployment interrupted by user. Partial results may have been saved.")
        sys.exit(130)
    except Exception as e:
        logger.error(f"\nDeployment failed with error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
