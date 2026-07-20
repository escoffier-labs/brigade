package main

import (
	"fmt"
	"os"

	"github.com/escoffier-labs/miseledger/internal/app"
)

func main() {
	args := os.Args[1:]
	if len(args) == 0 || args[0] == "--help" || args[0] == "-h" || args[0] == "help" {
		usage()
		os.Exit(0)
	}
	switch args[0] {
	case "version":
		os.Exit(app.Run([]string{"version"}, os.Stdout, os.Stderr))
	case "list", "search":
		os.Exit(app.Run(append([]string{"sessions"}, args...), os.Stdout, os.Stderr))
	default:
		// Treat bare terms as a search query: `sessionfind "release audit"`.
		os.Exit(app.Run(append([]string{"sessions", "search"}, args...), os.Stdout, os.Stderr))
	}
}

func usage() {
	fmt.Fprintln(os.Stdout, "sessionfind list [--source KIND] [--project NAME] [--model NAME] [--limit N] [--json]")
	fmt.Fprintln(os.Stdout, "sessionfind search <query> [--source KIND] [--project NAME] [--model NAME] [--limit N] [--json]")
	fmt.Fprintln(os.Stdout, "sessionfind <query> [--source KIND] [--project NAME] [--model NAME] [--limit N] [--json]")
}
