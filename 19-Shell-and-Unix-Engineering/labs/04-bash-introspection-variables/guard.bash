# guard.bash: the common "am I sourced?" test, which compares BASH_SOURCE[0] with $0.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  echo "BASH_SOURCE test: run as a program"
else
  echo "BASH_SOURCE test: sourced"
fi
