#!/bin/bash
S=/tmp/claude-1000/-home-tarrri-projects-ai-pers2/0f096e15-2848-4a40-a04c-9d1b64bb03d2/scratchpad
cd ~/projects/ai-pers2/ocdev && rm -rf $S/stand-tmp && mkdir -p $S/stand-tmp
V=~/projects/ai-pers2/integration-local-host/.venv/bin/python
PYTHONPATH=$($V -c "import sys; sys.path.insert(0,'scripts'); import check_combined as c, os; print(os.pathsep.join(str(c.ROOT/p) for p in c.SOURCES+c.HELPERS))") AI_PANEL_BROWSER_EXECUTABLE=/home/tarrri/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell timeout 300 $V $S/stand.py $PWD $S/ocdist $S/stand-tmp "$@" 2>&1 | grep -v -i "warning"
