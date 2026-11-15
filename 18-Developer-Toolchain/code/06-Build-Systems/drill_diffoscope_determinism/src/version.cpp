// Build stamp: the classic embedded-macro leak.
// __DATE__ and __TIME__ read the wall clock unless SOURCE_DATE_EPOCH is set;
// __FILE__ is the path exactly as the compiler was given it.
extern "C" const char* build_stamp() { return "engine built " __DATE__ " " __TIME__; }

extern "C" const char* version_source() { return __FILE__; }
