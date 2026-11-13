// Asymptotic complexity fitting, ported from Google Benchmark v1.10.0 src/complexity.cc.
//
// For each candidate curve g(n) the fit is the one-parameter least squares line through the
// origin, coef = sum(t * g) / sum(g * g), scored by the RMS of the residuals divided by the mean
// of the observations. o1 is the starting best fit; lgN, N, NlgN, N^2, N^3 replace it only when
// their normalized RMS is strictly lower (MinimalLeastSq with oAuto). nanobench v4.6.0
// (BigO::BigO in nanobench.h) computes the same coefficient and the same normalized RMS.
#pragma once

#include <cmath>
#include <cstddef>
#include <string>
#include <vector>

namespace lab {

enum class BigO { o1, oLogN, oN, oNLogN, oNSquared, oNCubed };

inline const char* big_o_string(BigO c) {
  switch (c) {
    case BigO::o1: return "(1)";
    case BigO::oLogN: return "lgN";
    case BigO::oN: return "N";
    case BigO::oNLogN: return "NlgN";
    case BigO::oNSquared: return "N^2";
    case BigO::oNCubed: return "N^3";
  }
  return "f(N)";
}

inline double curve(BigO c, double n) {
  switch (c) {
    case BigO::o1: return 1.0;
    case BigO::oLogN: return std::log2(n);
    case BigO::oN: return n;
    case BigO::oNLogN: return n * std::log2(n);
    case BigO::oNSquared: return std::pow(n, 2);
    case BigO::oNCubed: return std::pow(n, 3);
  }
  return 1.0;
}

struct Fit {
  BigO complexity = BigO::o1;
  double coef = 0.0;
  double rms = 0.0;  // normalized by the mean observation
};

inline Fit fit_one(const std::vector<double>& n, const std::vector<double>& t, BigO c) {
  double sigma_gn_squared = 0.0;
  double sigma_time = 0.0;
  double sigma_time_gn = 0.0;
  for (std::size_t i = 0; i < n.size(); ++i) {
    const double gn = curve(c, n[i]);
    sigma_gn_squared += gn * gn;
    sigma_time += t[i];
    sigma_time_gn += t[i] * gn;
  }
  Fit f;
  f.complexity = c;
  f.coef = sigma_time_gn / sigma_gn_squared;
  double rss = 0.0;
  for (std::size_t i = 0; i < n.size(); ++i) {
    const double r = t[i] - f.coef * curve(c, n[i]);
    rss += r * r;
  }
  const double mean = sigma_time / static_cast<double>(n.size());
  f.rms = mean == 0.0 ? 0.0 : std::sqrt(rss / static_cast<double>(n.size())) / mean;
  return f;
}

inline Fit fit_auto(const std::vector<double>& n, const std::vector<double>& t) {
  Fit best = fit_one(n, t, BigO::o1);
  for (BigO c : {BigO::oLogN, BigO::oN, BigO::oNLogN, BigO::oNSquared, BigO::oNCubed}) {
    const Fit f = fit_one(n, t, c);
    if (f.rms < best.rms) best = f;
  }
  return best;
}

}  // namespace lab
