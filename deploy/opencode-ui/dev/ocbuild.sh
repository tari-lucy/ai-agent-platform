#!/bin/bash
set -e
S=/tmp/claude-1000/-home-tarrri-projects-ai-pers2/0f096e15-2848-4a40-a04c-9d1b64bb03d2/scratchpad
export PATH=$HOME/.bun/bin:$PATH
cd ~/projects/ai-pers2/opencode-src/packages/app
rm -rf $S/ocdist
OPENCODE_CHANNEL=prod timeout 900 bun x vite build --base /opencode/ --outDir $S/ocdist 2>&1 | grep -E -A8 "built in|rror" | head -20
cd $S/ocdist || exit 1
[ "$(pwd)" = "$S/ocdist" ] || exit 1
find . -type f \( -name "*.aac" -o -name "*.mp4" -o -name "_headers" -o -name "*.map" \) -delete
echo "files $(find . -type f | wc -l) size $(du -sh . | cut -f1)"
cd ~/projects/ai-pers2/ocdev
V=~/projects/ai-pers2/integration-local-host/.venv/bin/python
PYTHONPATH=$($V -c "import sys; sys.path.insert(0,'scripts'); import check_combined as c, os; print(os.pathsep.join(str(c.ROOT/p) for p in c.SOURCES))") $V scripts/package_opencode_ui.py --build-dir $S/ocdist | tail -1
