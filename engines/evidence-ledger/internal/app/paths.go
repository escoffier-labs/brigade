package app

import (
	"os"
	"path/filepath"
)

type Paths struct {
	ConfigPath string `json:"config_path"`
	DataDir    string `json:"data_dir"`
	DBPath     string `json:"db_path"`
	CacheDir   string `json:"cache_dir"`
}

func ResolvePaths() Paths {
	home := os.Getenv("HOME")
	configHome := os.Getenv("XDG_CONFIG_HOME")
	if configHome == "" {
		configHome = filepath.Join(home, ".config")
	}
	dataHome := os.Getenv("XDG_DATA_HOME")
	if dataHome == "" {
		dataHome = filepath.Join(home, ".local", "share")
	}
	cacheHome := os.Getenv("XDG_CACHE_HOME")
	if cacheHome == "" {
		cacheHome = filepath.Join(home, ".cache")
	}
	dataDir := filepath.Join(dataHome, "logspine")
	return Paths{
		ConfigPath: filepath.Join(configHome, "logspine", "config.toml"),
		DataDir:    dataDir,
		DBPath:     filepath.Join(dataDir, "logspine.db"),
		CacheDir:   filepath.Join(cacheHome, "logspine"),
	}
}
