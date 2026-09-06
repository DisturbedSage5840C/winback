"""The one fact this test suite needed and didn't have: which machine trained the model.

`model_v1.json` and `calibrator_v1.joblib` were produced on macOS arm64 (see
`requirements.txt`'s header). XGBoost's histogram training and the floating-point
arithmetic underneath `predict_proba` are not guaranteed bit-identical across CPU
architectures — same seed, same rows, different silicon, different last few digits.
Structural claims (which calibrator won, whether two runs *on the same machine* agree)
hold everywhere and are tested everywhere. The handful of assertions that compare a
freshly computed digit against the exact digit committed from macOS arm64 only mean what
they claim to mean on that platform; see docs/WHAT_BROKE.md, "byte-for-byte reproducibility
in ml/__main__.py's docstring."
"""

from __future__ import annotations

import platform

ON_REFERENCE_PLATFORM = platform.system() == "Darwin" and platform.machine() == "arm64"

NOT_REFERENCE_PLATFORM_REASON = (
    "exact-digit comparison against the committed model, valid only on the platform "
    "that trained it (macOS arm64); see docs/WHAT_BROKE.md"
)
