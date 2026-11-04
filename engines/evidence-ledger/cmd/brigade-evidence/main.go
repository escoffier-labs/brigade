package main

import (
	"os"

	"github.com/escoffier-labs/brigade/engines/evidence-ledger/internal/app"
)

func main() {
	os.Exit(app.Main(os.Args, os.Stdout, os.Stderr))
}
