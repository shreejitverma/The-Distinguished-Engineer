# guard-return.bash: the "am I sourced?" test that asks whether return is legal here.
if (return 0 2>/dev/null); then
  echo "return test: sourced"
else
  echo "return test: run as a program"
fi
