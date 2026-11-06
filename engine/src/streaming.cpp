#include "mscan/streaming.h"

#include <cmath>
#include <stdexcept>
#include <string>

#include "mscan/detail/adx_terms.h"

namespace mscan::ind {
namespace {

std::size_t checked(int value, std::size_t max, const char* what) {
  if (value < 1 || static_cast<std::size_t>(value) > max) {
    throw std::invalid_argument(std::string("StreamingIndicators: ") + what + " must be in 1.." +
                                std::to_string(max));
  }
  return static_cast<std::size_t>(value);
}

}  // namespace

StreamingIndicators::StreamingIndicators(int roc_period, int obv_window)
    : roc_period_(checked(roc_period, kMaxRocPeriod, "roc_period")),
      obv_max_(checked(obv_window, kMaxObvWindow, "obv_window")) {}

// Each block mirrors one batch kernel's loop body for index i = count_, in the
// same order, so every rolling mean sees the same adds and removes.
void StreamingIndicators::update(double /*open*/, double high, double low, double close,
                                 double volume) {
  const std::size_t i = count_;
  const double prev_close = close_.ago(0);   // NaN on the first bar, like shift(1)

  // sma(): remove x[i - p] (still in the ring, p - 1 pushes back), then add x[i]
  auto step = [&](detail::PandasRollingMean& mean, std::size_t p) {
    if (i >= p) mean.remove(close_.ago(p - 1));
    mean.add(close);
  };
  step(sma20_, 20);
  step(sma50_, 50);
  step(sma200_, 200);
  ma50_hist_.push(sma50_.value());
  ma200_hist_.push(sma200_.value());

  // roc(): (close - prev) / prev * 100, prev = close roc_period bars back
  if (i >= roc_period_) {
    const double prev = close_.ago(roc_period_ - 1);
    roc_ = (close - prev) / prev * 100.0;
  }

  // adx(): the 14-bar-old terms are the oldest entries in the rings
  const double tr = detail::true_range(high, low, prev_close);
  const double pdm = detail::plus_dm(high, low, prev_high_, prev_low_);
  const double mdm = detail::minus_dm(high, low, prev_high_, prev_low_);
  if (i >= kAdxPeriod) {
    atr_.remove(tr_.ago(kAdxPeriod - 1));
    plus_mean_.remove(plus_dm_.ago(kAdxPeriod - 1));
    minus_mean_.remove(minus_dm_.ago(kAdxPeriod - 1));
  }
  atr_.add(tr);
  plus_mean_.add(pdm);
  minus_mean_.add(mdm);
  const double a = atr_.value();
  plus_di_ = 100.0 * (plus_mean_.value() / a);
  minus_di_ = 100.0 * (minus_mean_.value() / a);
  const double dx = detail::dx(plus_di_, minus_di_);
  if (i >= kAdxPeriod) adx_.remove(dx_.ago(kAdxPeriod - 1));
  adx_.add(dx);
  tr_.push(tr);
  plus_dm_.push(pdm);
  minus_dm_.push(mdm);
  dx_.push(dx);

  // obv(): direction from the close change, NaN flow leaves the total alone
  double direction = 0.0;
  if (close > prev_close) direction = 1.0;
  if (close < prev_close) direction = -1.0;
  const double flow = direction * volume;
  if (std::isnan(flow)) {
    obv_ = std::numeric_limits<double>::quiet_NaN();
  } else {
    obv_total_ += flow;
    obv_ = obv_total_;
  }
  obv_max_.push(obv_);

  close_.push(close);
  prev_high_ = high;
  prev_low_ = low;
  ++count_;
}

}  // namespace mscan::ind
