set -euo pipefail

brew_checkout=/usr/local/Homebrew
core_checkout="$brew_checkout/Library/Taps/homebrew/homebrew-core"
cask_checkout="$brew_checkout/Library/Taps/homebrew/homebrew-cask"

fail() {
  echo "$1" >&2
  exit 1
}

check_runner() {
  if [[ "${GITHUB_ACTIONS:-}" != true ]] \
    || [[ "${RUNNER_ENVIRONMENT:-}" != github-hosted ]] \
    || [[ "${RUNNER_OS:-}" != macOS ]] \
    || [[ "${RUNNER_ARCH:-}" != X64 ]]; then
    fail "A GitHub-hosted macOS Intel runner is required."
  fi

  [[ "$(/usr/bin/sw_vers -productVersion)" == 15.* ]] || fail "macOS 15 is required."
}

load_configuration() {
  local snapshot

  snapshot=$(
    /usr/bin/python3 - << 'PY'
from bottles.common import config

settings = config()
print(
    settings["brew_commit"],
    settings["core_commit"],
    "https://github.com/" + settings["core_repository"] + ".git",
)
PY
  )
  read -r brew_pin core_pin core_remote <<< "$snapshot"

  unset BOTTLE_CONFIG CORE_TOKEN

  export HOMEBREW_CORE_GIT_REMOTE="$core_remote"
  export HOMEBREW_NO_AUTO_UPDATE=1
  export HOMEBREW_NO_INSTALL_FROM_API=1
  export HOMEBREW_NO_ANALYTICS=1
  export HOMEBREW_NO_INSTALL_CLEANUP=1
  export HOMEBREW_CURL_RETRIES=4
}

network_brew() {
  BUNDLE_RETRY=0 /usr/bin/python3 -u -m bottles.network brew "$@"
}

check_homebrew() {
  if [[ "$(brew --prefix)" != /usr/local ]] \
    || [[ "$(brew --repository)" != "$brew_checkout" ]]; then
    fail "Expected Homebrew at /usr/local/Homebrew with prefix /usr/local."
  fi
}

create_backup() {
  backup="$(/usr/bin/mktemp -d "${RUNNER_TEMP:?}/bottle-runner.XXXXXXXX")"

  if command -v gh > /dev/null 2>&1; then
    /bin/cp "$(command -v gh)" "$backup/gh"
    echo "$backup" >> "${GITHUB_PATH:?}"
  fi
}

pin_homebrew() {
  local origin

  origin="$(git -C "$brew_checkout" remote get-url origin)"
  if [[ "$origin" != https://github.com/Homebrew/brew ]] \
    && [[ "$origin" != https://github.com/Homebrew/brew.git ]]; then
    fail "Homebrew must use the official Homebrew/brew remote."
  fi

  /usr/bin/python3 -u -m bottles.network \
    git -c http.version=HTTP/1.1 -C "$brew_checkout" fetch --no-tags --depth=1 origin "$brew_pin"
  git -C "$brew_checkout" checkout --detach "$brew_pin"
}

isolate_preinstalled_kegs() {
  local directory

  network_brew ruby -e '
    require "keg"
    HOMEBREW_CELLAR.glob("*/*/INSTALL_RECEIPT.json").each do |receipt|
      Keg.new(receipt.dirname).unlink
    end
  '

  for directory in \
    /usr/local/Cellar \
    /usr/local/opt \
    /usr/local/etc \
    /usr/local/var/homebrew/linked; do
    if [[ -e "$directory" ]]; then
      sudo /bin/mv "$directory" "$backup/$(basename "$directory")"
    fi

    sudo /bin/mkdir -p "$directory"
    sudo /usr/sbin/chown "$(id -u):$(id -g)" "$directory"
  done
}

checkout_core() {
  if [[ -e "$core_checkout" ]]; then
    /bin/mv "$core_checkout" "$backup/preinstalled-core"
  fi

  mkdir -p "$core_checkout"
  git -C "$core_checkout" init
  git -C "$core_checkout" remote add origin "$core_remote"
  /usr/bin/python3 -u -m bottles.network \
    git -c http.version=HTTP/1.1 -C "$core_checkout" fetch --no-tags --depth=1 origin "$core_pin"
  git -C "$core_checkout" checkout --detach "$core_pin"
}

checkout_casks() {
  if [[ -z "${CASK_COMMIT:-}" ]]; then
    return
  fi

  if [[ -e "$cask_checkout" ]]; then
    /bin/mv "$cask_checkout" "$backup/preinstalled-casks"
  fi

  mkdir -p "$cask_checkout"
  git -C "$cask_checkout" init
  git -C "$cask_checkout" remote add origin https://github.com/Homebrew/homebrew-cask.git
  /usr/bin/python3 -u -m bottles.network \
    git -c http.version=HTTP/1.1 -C "$cask_checkout" \
    fetch --no-tags --depth=1 origin "$CASK_COMMIT"
  git -C "$cask_checkout" checkout --detach "$CASK_COMMIT"
}

verify_checkouts() {
  if [[ "$(git -C "$brew_checkout" rev-parse HEAD)" != "$brew_pin" ]] \
    || [[ "$(git -C "$core_checkout" rev-parse HEAD)" != "$core_pin" ]]; then
    fail "Homebrew or Core does not match the selected commit."
  fi
}

prepare_commands() {
  echo /usr/bin >> "${GITHUB_PATH:?}"
  network_brew install-bundler-gems --add-groups=bottle,formula_test
  network_brew config

  echo "Runner setup complete. Preinstalled Homebrew files were moved to $backup"
}

check_runner
load_configuration
check_homebrew
create_backup
pin_homebrew
checkout_core
checkout_casks
verify_checkouts
isolate_preinstalled_kegs
prepare_commands
