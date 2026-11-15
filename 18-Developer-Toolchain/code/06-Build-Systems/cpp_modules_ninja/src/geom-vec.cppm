// Interface partition: contributes to geom's interface and is re-exported by geom.cppm.
export module geom:vec;
export struct Vec { int x; int y; };
export int dot(Vec a, Vec b) { return a.x * b.x + a.y * b.y; }
