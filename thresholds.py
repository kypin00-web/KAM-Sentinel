"""
SYS//MONITOR - Smart Warning Thresholds
Detects hardware and sets intelligent defaults based on known safe limits.
User-customizable values are saved to profiles/thresholds.json
"""

import json
import os

# ── Known CPU thermal limits (TJmax) ─────────────────────────────────────────
CPU_THERMAL_MAP = {
    # AMD Ryzen 5000 series
    "ryzen 7 5800x":  {"temp_warn": 75, "temp_crit": 90, "volt_min": 0.9, "volt_max": 1.45},
    "ryzen 9 5900x":  {"temp_warn": 75, "temp_crit": 90, "volt_min": 0.9, "volt_max": 1.45},
    "ryzen 9 5950x":  {"temp_warn": 75, "temp_crit": 90, "volt_min": 0.9, "volt_max": 1.45},
    "ryzen 5 5600x":  {"temp_warn": 75, "temp_crit": 90, "volt_min": 0.9, "volt_max": 1.45},
    # AMD Ryzen 7000 series
    "ryzen 9 7950x":  {"temp_warn": 85, "temp_crit": 95, "volt_min": 0.9, "volt_max": 1.35},
    "ryzen 9 7900x":  {"temp_warn": 85, "temp_crit": 95, "volt_min": 0.9, "volt_max": 1.35},
    "ryzen 7 7700x":  {"temp_warn": 85, "temp_crit": 95, "volt_min": 0.9, "volt_max": 1.35},
    # AMD Ryzen 3000 series
    "ryzen 9 3900x":  {"temp_warn": 70, "temp_crit": 85, "volt_min": 0.9, "volt_max": 1.45},
    "ryzen 7 3700x":  {"temp_warn": 70, "temp_crit": 85, "volt_min": 0.9, "volt_max": 1.45},
    # Intel 12th/13th gen
    "i9-13900":       {"temp_warn": 90, "temp_crit": 100,"volt_min": 0.9, "volt_max": 1.52},
    "i9-12900":       {"temp_warn": 85, "temp_crit": 100,"volt_min": 0.9, "volt_max": 1.52},
    "i7-13700":       {"temp_warn": 85, "temp_crit": 100,"volt_min": 0.9, "volt_max": 1.52},
    "i7-12700":       {"temp_warn": 80, "temp_crit": 100,"volt_min": 0.9, "volt_max": 1.52},
    "i5-13600":       {"temp_warn": 80, "temp_crit": 100,"volt_min": 0.9, "volt_max": 1.52},
    # Intel 10th/11th gen
    "i9-10900":       {"temp_warn": 80, "temp_crit": 100,"volt_min": 0.9, "volt_max": 1.52},
    "i7-10700":       {"temp_warn": 80, "temp_crit": 100,"volt_min": 0.9, "volt_max": 1.52},
}

# ── Known GPU thermal limits ──────────────────────────────────────────────────
GPU_THERMAL_MAP = {
    # NVIDIA RTX 4000 series
    "rtx 4090":  {"temp_warn": 80, "temp_crit": 90},
    "rtx 4080":  {"temp_warn": 80, "temp_crit": 90},
    "rtx 4070":  {"temp_warn": 80, "temp_crit": 90},
    "rtx 4060":  {"temp_warn": 80, "temp_crit": 90},
    # NVIDIA RTX 3000 series
    "rtx 3090":  {"temp_warn": 80, "temp_crit": 93},
    "rtx 3080":  {"temp_warn": 80, "temp_crit": 93},
    "rtx 3070":  {"temp_warn": 80, "temp_crit": 93},
    "rtx 3060":  {"temp_warn": 80, "temp_crit": 93},
    # NVIDIA RTX 2000 series
    "rtx 2080":  {"temp_warn": 80, "temp_crit": 94},
    "rtx 2070":  {"temp_warn": 80, "temp_crit": 94},
    "rtx 2060":  {"temp_warn": 80, "temp_crit": 94},
    # AMD RX 6000 series
    "rx 6900":   {"temp_warn": 80, "temp_crit": 110},
    "rx 6800":   {"temp_warn": 80, "temp_crit": 110},
    "rx 6700":   {"temp_warn": 80, "temp_crit": 110},
    "rx 6600":   {"temp_warn": 80, "temp_crit": 110},
    # AMD RX 7000 series
    "rx 7900":   {"temp_warn": 80, "temp_crit": 110},
    "rx 7800":   {"temp_warn": 80, "temp_crit": 110},
    "rx 7700":   {"temp_warn": 80, "temp_crit": 110},
}

# ── Generic fallback thresholds ───────────────────────────────────────────────
DEFAULT_THRESHOLDS = {
    "cpu": {
        "temp_warn":        75,
        "temp_crit":        90,
        "volt_min":         0.9,
        "volt_max":         1.45,
        "usage_warn":       85,
        "usage_crit":       95,
        "usage_sustain_sec": 30,   # seconds of sustained high usage before alerting
    },
    "gpu": {
        "temp_warn":        80,
        "temp_crit":        95,
        "usage_warn":       90,
        "usage_crit":       98,
        "usage_sustain_sec": 30,
    },
    "ram": {
        "usage_warn":       80,
        "usage_crit":       92,
    },
    "network": {
        "spike_multiplier": 5.0,   # alert if speed spikes > Nx baseline avg
        "baseline_samples": 12,    # number of samples to compute baseline
    },
    "voltage": {
        "cpu_min":  0.9,
        "cpu_max":  1.45,
    }
}


def detect_thresholds(cpu_name: str, gpu_name: str) -> dict:
    """
    Returns smart threshold defaults based on detected hardware.
    Falls back to generic defaults for unknown hardware.
    """
    thresholds = json.loads(json.dumps(DEFAULT_THRESHOLDS))  # deep copy

    cpu_lower = (cpu_name or '').lower()
    gpu_lower = (gpu_name or '').lower()

    # Match CPU
    for key, vals in CPU_THERMAL_MAP.items():
        if key in cpu_lower:
            thresholds['cpu']['temp_warn'] = vals['temp_warn']
            thresholds['cpu']['temp_crit'] = vals['temp_crit']
            thresholds['cpu']['volt_min']  = vals['volt_min']
            thresholds['cpu']['volt_max']  = vals['volt_max']
            thresholds['voltage']['cpu_min'] = vals['volt_min']
            thresholds['voltage']['cpu_max'] = vals['volt_max']
            break

    # Match GPU
    for key, vals in GPU_THERMAL_MAP.items():
        if key in gpu_lower:
            thresholds['gpu']['temp_warn'] = vals['temp_warn']
            thresholds['gpu']['temp_crit'] = vals['temp_crit']
            break

    thresholds['_detected_from'] = {
        'cpu': cpu_name or 'Unknown',
        'gpu': gpu_name or 'Unknown',
    }
    return thresholds


def load_thresholds(profile_dir: str, cpu_name: str, gpu_name: str) -> dict:
    """Load saved thresholds or generate smart defaults on first run."""
    path = os.path.join(profile_dir, 'thresholds.json')
    if os.path.exists(path):
        with open(path) as f:
            saved = json.load(f)
        # Ensure any new keys added in updates are present
        defaults = detect_thresholds(cpu_name, gpu_name)
        for section, vals in defaults.items():
            if section not in saved:
                saved[section] = vals
            elif isinstance(vals, dict):
                for k, v in vals.items():
                    if k not in saved[section]:
                        saved[section][k] = v
        return saved
    else:
        defaults = detect_thresholds(cpu_name, gpu_name)
        save_thresholds(profile_dir, defaults)
        return defaults


def save_thresholds(profile_dir: str, thresholds: dict):
    """Persist thresholds to disk."""
    os.makedirs(profile_dir, exist_ok=True)
    path = os.path.join(profile_dir, 'thresholds.json')
    with open(path, 'w') as f:
        json.dump(thresholds, f, indent=2)
