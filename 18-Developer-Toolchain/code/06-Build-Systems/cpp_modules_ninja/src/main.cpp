// Ordinary translation unit that imports a module; still scanned, provides nothing.
#include <cstdio>
import report;
int main() {
  std::printf("summary() = %d\n", summary());
  return 0;
}
