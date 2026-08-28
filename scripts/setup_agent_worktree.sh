#!/bin/bash
# Creates a sibling git worktree for parallel feature development with its
# own branch and .venv, and a copy of .env if one exists. Ready for a
# separate Ghostty + PyCharm window pair. See .claude/skills/setup-worktrees/.
set -euo pipefail

AGENT_NAME=${1:?"usage: setup_agent_worktree.sh <branch-name> [base-branch]"}
BASE_BRANCH=${2:-main}
REPO_ROOT=$(git rev-parse --show-toplevel)
WORKTREE_DIR="${REPO_ROOT}/../worktree-${AGENT_NAME//\//-}"

echo "=== Creating worktree for ${AGENT_NAME} from ${BASE_BRANCH} ==="

if [[ -e "${WORKTREE_DIR}" ]]; then
  echo "Error: ${WORKTREE_DIR} already exists." >&2
  exit 1
fi

git -C "${REPO_ROOT}" worktree add "${WORKTREE_DIR}" -b "${AGENT_NAME}" "${BASE_BRANCH}"

cd "${WORKTREE_DIR}"

if [[ -f "${REPO_ROOT}/.env" ]]; then
  cp "${REPO_ROOT}/.env" .env
  chmod 600 .env
fi

python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate

cat <<EOF

Worktree ready: ${WORKTREE_DIR}
  Branch: ${AGENT_NAME}
  Base:   ${BASE_BRANCH}
  .venv:  created
  .env:   $( [[ -f .env ]] && echo copied || echo "skipped (no .env in ${REPO_ROOT})" )

PyCharm: File -> Open -> ${WORKTREE_DIR} (set interpreter to its .venv)
Ghostty: cd ${WORKTREE_DIR} && source .venv/bin/activate && ./bin/cstats

EOF
