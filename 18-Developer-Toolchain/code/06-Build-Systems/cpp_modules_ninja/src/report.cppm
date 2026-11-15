// Imports geom without re-exporting it, so geom is not part of report's interface.
export module report;
import geom;
export int summary() { return dot(Vec{3, 4}, Vec{5, 6}) + area(7, 8); }
