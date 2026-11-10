# Intelbrew

Prebuilt Homebrew bottles for my own use on macOS 15 Intel.

**Only packages selected for my own use and their required dependencies are prebuilt.**

[Releases](https://github.com/{{core_repository}}/releases) · [Builds](https://github.com/{{builder_repository}}/actions)

## Configuration

Add to `~/.homebrew/brew.env`:

```text
HOMEBREW_NO_INSTALL_FROM_API=1
HOMEBREW_CORE_GIT_REMOTE=https://github.com/{{core_repository}}
```

With `XDG_CONFIG_HOME` set, use `$XDG_CONFIG_HOME/homebrew/brew.env` instead.

## Existing Homebrew

```sh
brew tap --force homebrew/core
brew update
```

## New Homebrew installation

```sh
export HOMEBREW_NO_INSTALL_FROM_API=1
export HOMEBREW_CORE_GIT_REMOTE="https://github.com/{{core_repository}}"
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow the installer's next steps to configure your shell, then run:

```sh
eval "$(/usr/local/bin/brew shellenv)"
brew update
```

## Usage

Verify the Core remote:

```sh
git -C "$(brew --repository homebrew/core)" remote get-url origin
```

It should be `https://github.com/{{core_repository}}`. Continue using normal commands:

```sh
brew update
brew install <formula>
brew upgrade
```

## Return to official Core

In your `brew.env`, remove `HOMEBREW_NO_INSTALL_FROM_API` and change the Core URL to `https://github.com/Homebrew/homebrew-core`. In the current terminal, run:

```sh
unset HOMEBREW_NO_INSTALL_FROM_API
export HOMEBREW_CORE_GIT_REMOTE="https://github.com/Homebrew/homebrew-core"
brew update
```
