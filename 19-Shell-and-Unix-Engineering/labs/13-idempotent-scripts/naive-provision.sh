#!/bin/sh
# naive-provision.sh ROOT: the desired state of converge.sh, written as one-shot commands.
# Every line assumes it runs exactly once against a fresh ROOT.
# A second run exits 0 and corrupts ROOT: cp -R nests, sed compounds, >> duplicates, ln nests.
# Usage: naive-provision.sh ROOT
cd "$1" || exit 1
cp -R skel conf
sed 's/80/8080/' conf/app.conf >conf/app.conf.tmp
mv conf/app.conf.tmp conf/app.conf
echo '10.0.0.1 db' >>hosts
rm run/stale.pid
ln -s releases/v1 current
