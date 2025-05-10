# Repository Hosting Pipeline

A robust pipeline for automatically deploying multiple GitHub repositories to hosting platforms like Netlify, Render, and GitHub Pages.

## Features

- **Multi-platform Support**: Deploy to Netlify, Render, or GitHub Pages
- **Rate Limit Handling**: Built-in exponential backoff for API rate limits
- **Resumable Deployments**: Can pick up where it left off if interrupted
- **Detailed Logging**: Comprehensive logging of all operations
- **Configurable Options**: Customize deployment settings
- **CSV Output**: Generates a CSV file with repository URLs and their corresponding hosted URLs

## Requirements

- Python 3.6+
- Required Python packages (install with `pip install -r requirements.txt`):
  - requests
  - pandas
  - tqdm
  - python-dotenv

## Setup

1. Clone this repository:
   ```
   git clone <repository-url>
   cd repo-hosting-pipeline
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set up API tokens:
   - Copy `.env.sample` to `.env`
   - Fill in your API tokens for the platforms you want to use:
     - Netlify: Get a personal access token from https://app.netlify.com/user/applications#personal-access-tokens
     - Render: Get an API key from https://render.com/docs/api
     - GitHub: Create a personal access token with repo permissions from https://github.com/settings/tokens

## Usage

### Basic Usage

```bash
python repo_hosting_pipeline.py sample_repos.txt
```

This will read repository URLs from `sample_repos.txt` and deploy them to Netlify (default platform).

### Advanced Usage

```bash
python repo_hosting_pipeline.py sample_repos.txt --platforms netlify render github --output results.csv --delay 15 --max-retries 5 --verbose
```

### Command Line Arguments

- `input_file`: Text file containing GitHub repository URLs (one per line)
- `--output`, `-o`: Output CSV file (default: `deployment_results.csv`)
- `--platforms`, `-p`: Platforms to deploy to (choices: `netlify`, `render`, `github`; default: `netlify`)
- `--max-retries`, `-r`: Maximum number of retry attempts for failed deployments (default: 3)
- `--delay`, `-d`: Delay between deployments in seconds to avoid rate limits (default: 10)
- `--max-size`, `-s`: Maximum size of deployment package in MB (default: 90)
- `--resume-from`, `-f`: Repository URL to resume from (skip all repositories before this one)
- `--verbose`, `-v`: Enable verbose logging

## Input File Format

The input file should contain one GitHub repository URL per line:

```
https://github.com/username/repo1.git
https://github.com/username/repo2.git
https://github.com/username/repo3.git
```

## Output Format

The pipeline generates a CSV file with the following columns:

- `repo_url`: The original GitHub repository URL
- `hosted_url`: The URL where the repository is hosted
- `platform`: The platform where the repository is hosted (netlify, render, or github)
- `status`: Success or failure status
- `notes`: Additional information about the deployment

## Example Workflow

1. Create a text file with repository URLs:
   ```
   echo "https://github.com/username/repo1.git" > repos.txt
   echo "https://github.com/username/repo2.git" >> repos.txt
   ```

2. Run the pipeline:
   ```
   python repo_hosting_pipeline.py repos.txt --platforms netlify github
   ```

3. Check the results in `deployment_results.csv`

## Troubleshooting

- **Rate Limits**: If you're deploying many repositories, you might hit rate limits. Increase the `--delay` value.
- **Authentication Errors**: Make sure your API tokens are correct and have the necessary permissions.
- **Deployment Failures**: Check the logs for specific error messages. Some repositories might not be suitable for static hosting.

## License

MIT
