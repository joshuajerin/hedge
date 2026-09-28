#!/bin/zsh
set -euo pipefail

if (( $# != 1 )); then
  print -u2 "usage: $0 <keychain-service-name>"
  exit 64
fi

service_name="$1"
print -n "Paste the fresh Slack value for $service_name: "
read -rs secret_value
print
[[ -n "$secret_value" ]] || { print -u2 "A value is required."; exit 64; }

security add-generic-password -U -a "$USER" -s "$service_name" -w "$secret_value" >/dev/null
unset secret_value
print "Stored in the login Keychain."
