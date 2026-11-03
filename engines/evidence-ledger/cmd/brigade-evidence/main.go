package main

import (
	"os"

	"github.com/escoffier-labs/miseledger/internal/app"
)

func main() {
	os.Exit(app.Main(os.Args, os.Stdout, os.Stderr))
}
