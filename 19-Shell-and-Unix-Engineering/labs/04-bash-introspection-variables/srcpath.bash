# srcpath.bash: report whether BASH_SOURCE[0] holds an absolute or a relative path.
case ${BASH_SOURCE[0]} in
  /*) echo "BASH_SOURCE[0] is absolute" ;;
  *) echo "BASH_SOURCE[0] is relative: ${BASH_SOURCE[0]}" ;;
esac
