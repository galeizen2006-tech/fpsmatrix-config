// Configuración de fábrica: se usa si todavía no se pudo leer GitHub ni hay copia en KV.
// Es una copia de ../config.json (regenerar con: npm run sync-config).
export default {
  "schema_version": 1,
  "config_version": 2,
  "updated_at": "2026-10-19T20:15:00Z",
  "app": {
    "latest_version": "1.9.5",
    "min_supported_version": "1.9.0",
    "download_url": "https://github.com/galeizen2006-tech/nivelgamer-releases",
    "release_notes": {
      "es": "Benchmark más detallado y optimizaciones para todos los juegos.",
      "en": "More detailed benchmark and optimizations for every game."
    }
  },
  "compatibility": {
    "os": [
      "windows-10",
      "windows-11"
    ],
    "min_windows_build": 19041,
    "blocked_app_versions": []
  },
  "kill_switch": {
    "enabled": false,
    "message": {}
  },
  "features": {
    "benchmark": {
      "enabled": true,
      "tier": "free",
      "description": "Benchmark de CPU, GPU, RAM y disco."
    },
    "community_fps": {
      "enabled": false,
      "tier": "free",
      "min_app_version": "1.9.4",
      "sensitive": true,
      "description": "Compartir FPS anónimos con la comunidad. Requiere consentimiento del usuario."
    },
    "game_mode_priority": {
      "enabled": true,
      "tier": "free",
      "min_app_version": "1.9.5",
      "sensitive": true,
      "description": "Modo partida: baja la prioridad de apps en segundo plano. Pide permiso antes de aplicarse."
    },
    "advanced_reports": {
      "enabled": false,
      "tier": "premium",
      "description": "Ejemplo de función premium futura (apagada)."
    }
  },
  "parameters": {
    "benchmark.duration_seconds": 30,
    "benchmark.disk_test_mb": 512,
    "fps.competitive_cap_hz": 360,
    "network.ping_samples": 10,
    "drivers.nvidia_recommended": "566.36",
    "drivers.amd_recommended": "24.10.1",
    "ui.show_tips": true
  },
  "banners": [],
  "private": {
    "notes": "Esta sección NUNCA se muestra en los endpoints públicos.",
    "experiments.next_release": "1.9.6"
  }
};
