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
	// Source is "canonical", "legacy" or "new" (docs/phase-engine-rename.md,
	// "Resolution rule"). LegacyPresent stays true whenever the legacy layout
	// exists, so a caller can warn when both layouts exist.
	Source        string `json:"-"`
	LegacyPresent bool   `json:"-"`
}

// evidenceLayout is one storage layout. Every path in a resolved Paths comes
// from the same layout, so the database, MAC key, config and cache never mix.
type evidenceLayout struct {
	leaf   []string
	dbName string
}

var (
	canonicalLayout = evidenceLayout{leaf: []string{"brigade", "evidence"}, dbName: "evidence.db"}
	// legacyLayout is read and written in place while it exists and the
	// canonical layout does not. Nothing is copied or moved.
	legacyLayout = evidenceLayout{leaf: []string{"miseledger"}, dbName: "miseledger.db"}
)

// ResolvePaths applies the shared resolution rule to the whole directory set:
// the canonical layout wins when it exists, the legacy layout is used in place
// when only it exists, and otherwise the canonical layout is new. A layout
// exists when its data or config directory exists. The cache is disposable and
// never decides the layout. The same rule lives in src/brigade/engine_paths.py.
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
	exists := func(layout evidenceLayout) bool {
		return isDir(layout.dir(dataHome)) || isDir(layout.dir(configHome))
	}
	legacyPresent := exists(legacyLayout)
	layout, source := canonicalLayout, "new"
	switch {
	case exists(canonicalLayout):
		source = "canonical"
	case legacyPresent:
		layout, source = legacyLayout, "legacy"
	}
	dataDir := layout.dir(dataHome)
	return Paths{
		ConfigPath:    filepath.Join(layout.dir(configHome), "config.toml"),
		DataDir:       dataDir,
		DBPath:        filepath.Join(dataDir, layout.dbName),
		CacheDir:      layout.dir(cacheHome),
		Source:        source,
		LegacyPresent: legacyPresent,
	}
}

func (l evidenceLayout) dir(root string) string {
	return filepath.Join(append([]string{root}, l.leaf...)...)
}

func isDir(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}
