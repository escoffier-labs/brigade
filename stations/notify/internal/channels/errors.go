package channels

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"strings"
)

// DeliveryError is a bounded, credential-free description of a channel
// failure. It intentionally does not retain or unwrap the original error.
type DeliveryError struct {
	Provider string
	Stage    string
	Status   int
	Cause    string
}

func (e *DeliveryError) Error() string {
	provider := boundedValue(e.Provider, "discord", "telegram", "signal")
	stage := boundedValue(
		e.Stage,
		"encode",
		"request",
		"send",
		"response",
		"dispatch",
	)
	cause := boundedValue(
		e.Cause,
		"encoding",
		"invalid_request",
		"dns",
		"tls",
		"timeout",
		"canceled",
		"connection",
		"http_status",
	)
	if e.Status >= 100 && e.Status <= 599 {
		return fmt.Sprintf(
			"provider=%s stage=%s status=%d cause=%s",
			provider,
			stage,
			e.Status,
			cause,
		)
	}
	return fmt.Sprintf("provider=%s stage=%s cause=%s", provider, stage, cause)
}

func boundedValue(value string, allowed ...string) string {
	for _, candidate := range allowed {
		if value == candidate {
			return value
		}
	}
	return "unknown"
}

func transportError(provider, stage string, err error) error {
	return &DeliveryError{
		Provider: provider,
		Stage:    stage,
		Cause:    classifyCause(err),
	}
}

func requestError(provider string) error {
	return &DeliveryError{
		Provider: provider,
		Stage:    "request",
		Cause:    "invalid_request",
	}
}

func encodingError(provider string) error {
	return &DeliveryError{
		Provider: provider,
		Stage:    "encode",
		Cause:    "encoding",
	}
}

func statusError(provider string, status int) error {
	return &DeliveryError{
		Provider: provider,
		Stage:    "response",
		Status:   status,
		Cause:    "http_status",
	}
}

// SafeError renders an error for an output boundary. Unknown error types are
// reduced to a cause class; their original text is never returned.
func SafeError(err error) string {
	if err == nil {
		return ""
	}
	var deliveryErr *DeliveryError
	if errors.As(err, &deliveryErr) {
		return deliveryErr.Error()
	}
	return (&DeliveryError{
		Stage: "dispatch",
		Cause: classifyCause(err),
	}).Error()
}

func classifyCause(err error) string {
	if err == nil {
		return "unknown"
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "timeout"
	}
	if errors.Is(err, context.Canceled) {
		return "canceled"
	}

	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return "dns"
	}
	var unknownAuthority x509.UnknownAuthorityError
	var hostnameError x509.HostnameError
	var certificateInvalid x509.CertificateInvalidError
	var recordHeader tls.RecordHeaderError
	if errors.As(err, &unknownAuthority) ||
		errors.As(err, &hostnameError) ||
		errors.As(err, &certificateInvalid) ||
		errors.As(err, &recordHeader) {
		return "tls"
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "timeout"
	}
	var opErr *net.OpError
	if errors.As(err, &opErr) {
		return "connection"
	}

	// Some standard-library TLS and syscall errors have no exported concrete
	// type. Their text is inspected only to choose a fixed label and is never
	// included in the returned error.
	message := strings.ToLower(err.Error())
	switch {
	case strings.Contains(message, "tls"),
		strings.Contains(message, "x509"),
		strings.Contains(message, "certificate"),
		strings.Contains(message, "handshake"):
		return "tls"
	case strings.Contains(message, "no such host"),
		strings.Contains(message, "server misbehaving"),
		strings.Contains(message, "lookup "):
		return "dns"
	case strings.Contains(message, "timeout"),
		strings.Contains(message, "deadline exceeded"):
		return "timeout"
	case strings.Contains(message, "connection"),
		strings.Contains(message, "broken pipe"),
		strings.Contains(message, "network is unreachable"):
		return "connection"
	default:
		return "unknown"
	}
}
