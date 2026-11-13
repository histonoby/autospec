# 飽和演算モジュール 仕様書

`sat.h` で宣言する2つの関数を実装する。

- REQ-001: `sat_add16(a, b)` は、a + b が int16_t の範囲内ならその和を返す。
- REQ-002: `sat_add16(a, b)` は、和が INT16_MAX を超えたら INT16_MAX、INT16_MIN を下回ったら INT16_MIN を返す（飽和）。
- REQ-003: `clamp_int(v, lo, hi)` は v を [lo, hi] に制限した値を返す（lo <= hi を前提とする）。
