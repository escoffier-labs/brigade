package app

import (
	"fmt"
	"strings"
)

func splitFlags(args []string, valueFlags, boolFlags map[string]bool) (map[string]string, map[string]bool, []string, error) {
	values := map[string]string{}
	bools := map[string]bool{}
	var rest []string
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if !strings.HasPrefix(arg, "--") || arg == "--" {
			rest = append(rest, arg)
			continue
		}
		nameVal := strings.TrimPrefix(arg, "--")
		name := nameVal
		val := ""
		if idx := strings.IndexByte(nameVal, '='); idx >= 0 {
			name = nameVal[:idx]
			val = nameVal[idx+1:]
		}
		if valueFlags != nil && valueFlags[name] {
			if val == "" {
				i++
				if i >= len(args) {
					return nil, nil, nil, fmt.Errorf("--%s requires a value", name)
				}
				val = args[i]
			}
			values[name] = val
			continue
		}
		if boolFlags != nil && boolFlags[name] {
			if val != "" {
				return nil, nil, nil, fmt.Errorf("--%s does not take a value", name)
			}
			bools[name] = true
			continue
		}
		return nil, nil, nil, fmt.Errorf("unknown flag --%s", name)
	}
	return values, bools, rest, nil
}

func splitWrapperFlags(args []string) (bool, bool, []string) {
	bools, pass := splitExactBoolPassthrough(args, "json", "dry-run")
	return bools["json"], bools["dry-run"], pass
}

func splitExactBoolPassthrough(args []string, names ...string) (map[string]bool, []string) {
	known := map[string]bool{}
	for _, name := range names {
		known[name] = true
	}
	bools := map[string]bool{}
	pass := make([]string, 0, len(args))
	for _, arg := range args {
		name, ok := exactLongFlagName(arg)
		if ok && known[name] {
			bools[name] = true
			continue
		}
		pass = append(pass, arg)
	}
	return bools, pass
}

func stripValueFlag(args []string, name string) []string {
	out := make([]string, 0, len(args))
	prefix := "--" + name + "="
	for i := 0; i < len(args); i++ {
		if args[i] == "--"+name {
			i++
			continue
		}
		if strings.HasPrefix(args[i], prefix) {
			continue
		}
		out = append(out, args[i])
	}
	return out
}

func stripBoolFlag(args []string, name string) []string {
	out := make([]string, 0, len(args))
	long := "--" + name
	for _, arg := range args {
		if arg == long {
			continue
		}
		out = append(out, arg)
	}
	return out
}

func hasBoolFlag(args []string, name string) bool {
	long := "--" + name
	for _, arg := range args {
		if arg == long {
			return true
		}
	}
	return false
}

func hasFlag(args []string, name string) bool {
	long := "--" + name
	prefix := long + "="
	for _, arg := range args {
		if arg == long || strings.HasPrefix(arg, prefix) {
			return true
		}
	}
	return false
}

func firstPositional(args []string) string {
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if !strings.HasPrefix(arg, "--") || arg == "--" {
			return arg
		}
		nameVal := strings.TrimPrefix(arg, "--")
		name := nameVal
		hasInlineValue := false
		if idx := strings.IndexByte(nameVal, '='); idx >= 0 {
			name = nameVal[:idx]
			hasInlineValue = true
		}
		if hasInlineValue || isKnownBoolFlag(name) {
			continue
		}
		i++
	}
	return ""
}

func exactLongFlagName(arg string) (string, bool) {
	if !strings.HasPrefix(arg, "--") || arg == "--" {
		return "", false
	}
	name := strings.TrimPrefix(arg, "--")
	if name == "" || strings.Contains(name, "=") {
		return "", false
	}
	return name, true
}
