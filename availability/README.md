# Availability

Python script that reports GitHub PR and review workload.

## Requirements

- Python 3
- Git
- [GitHub CLI (`gh`)](https://cli.github.com/)

Authenticate `gh` first:

```bash
gh auth login
gh auth status
```

## Usage

The script **must be run from the root of the Git repository** you want to check:

```bash
cd /path/to/repository
python3 /path/to/script/main.py
```

The repository is detected automatically from the Git `origin` remote.

## Configuration

The script optionally reads a `.env` file.

`USER_WHITELIST_REGEX` - used to identify users by username or organization name

## Optional `availability` shortcut

From the directory containing `main.py`:

### Bashrc

 ```bash
printf "\nalias availability='python3 %s/main.py'\n" "$PWD" >> ~/.bashrc
source ~/.bashrc
```

### Zshrc

```bash
printf "\nalias availability='python3 %s/main.py'\n" "$PWD" >> ~/.zshrc
source ~/.zshrc
```

Then run from the repository:

```bash
cd /path/to/repository
availability
```
