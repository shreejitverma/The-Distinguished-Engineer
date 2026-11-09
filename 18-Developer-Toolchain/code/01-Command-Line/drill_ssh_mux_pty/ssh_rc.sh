#!/bin/sh
# The remote ~/.ssh/rc from the note: sshd runs it for every session, so the symlink always
# points at the agent socket of the newest login.
if [ -n "$SSH_AUTH_SOCK" ] && [ "$SSH_AUTH_SOCK" != "$HOME/.ssh/ssh_auth_sock" ]; then
    mkdir -p "$HOME/.ssh"
    ln -snf "$SSH_AUTH_SOCK" "$HOME/.ssh/ssh_auth_sock"
fi
