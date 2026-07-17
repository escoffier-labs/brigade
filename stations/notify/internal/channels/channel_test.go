package channels

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/escoffier-labs/agent-notify/internal/canonical"
)

const sentinelCredential = "SENTINEL-CREDENTIAL-DO-NOT-PRINT"

type errorTransport struct {
	err error
}

func (t errorTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, t.err
}

type timeoutError struct{}

func (timeoutError) Error() string   { return "timeout " + sentinelCredential }
func (timeoutError) Timeout() bool   { return true }
func (timeoutError) Temporary() bool { return true }

type fakeChannel struct {
	sent []canonical.Message
	err  error
}

func (f *fakeChannel) Name() string { return "fake" }
func (f *fakeChannel) Type() string { return "fake" }
func (f *fakeChannel) Send(_ context.Context, m canonical.Message) error {
	if f.err != nil {
		return f.err
	}
	f.sent = append(f.sent, m)
	return nil
}

func TestRegistryRegisterAndGet(t *testing.T) {
	r := NewRegistry()
	fc := &fakeChannel{}
	r.Register("a", fc)
	got, ok := r.Get("a")
	if !ok {
		t.Fatal("expected channel registered")
	}
	if got.Name() != "fake" {
		t.Errorf("expected name=fake, got %s", got.Name())
	}
}

func TestRegistryGet_MissingReturnsFalse(t *testing.T) {
	r := NewRegistry()
	if _, ok := r.Get("missing"); ok {
		t.Fatal("expected ok=false for missing channel")
	}
}

func TestRegistryAll_ReturnsRegisteredNames(t *testing.T) {
	r := NewRegistry()
	r.Register("a", &fakeChannel{})
	r.Register("b", &fakeChannel{})
	names := r.AllNames()
	if len(names) != 2 {
		t.Fatalf("expected 2 names, got %d", len(names))
	}
}

func TestChannelTransportErrorsAreBoundedAndCredentialFree(t *testing.T) {
	causes := []struct {
		name string
		err  error
		want string
	}{
		{
			name: "dns",
			err:  &net.DNSError{Err: "no such host", Name: sentinelCredential},
			want: "dns",
		},
		{
			name: "tls",
			err: x509.UnknownAuthorityError{
				Cert: &x509.Certificate{RawIssuer: []byte("test")},
			},
			want: "tls",
		},
		{
			name: "timeout",
			err:  timeoutError{},
			want: "timeout",
		},
		{
			name: "connection",
			err: &net.OpError{
				Op:  "dial",
				Net: "tcp",
				Err: fmt.Errorf("connection refused: %s", sentinelCredential),
			},
			want: "connection",
		},
	}

	for _, cause := range causes {
		for _, provider := range []string{"discord", "telegram", "signal"} {
			t.Run(provider+"/"+cause.name, func(t *testing.T) {
				var channel Channel
				switch provider {
				case "discord":
					candidate := NewDiscord(
						"discord-main",
						"https://example.invalid/hooks/"+sentinelCredential+"?token="+sentinelCredential,
						time.Second,
					)
					candidate.client.Transport = errorTransport{err: cause.err}
					channel = candidate
				case "telegram":
					candidate := NewTelegram(
						"telegram-main",
						"https://example.invalid",
						sentinelCredential,
						"123",
						time.Second,
					)
					candidate.client.Transport = errorTransport{err: cause.err}
					channel = candidate
				case "signal":
					candidate := NewSignal(
						"signal-main",
						"https://example.invalid/api/"+sentinelCredential+"?token="+sentinelCredential,
						"+15550001111",
						"recipient",
						time.Second,
					)
					candidate.client.Transport = errorTransport{err: cause.err}
					channel = candidate
				}

				err := channel.Send(context.Background(), canonical.Message{Body: "test"})
				if err == nil {
					t.Fatal("expected transport error")
				}
				if strings.Contains(err.Error(), sentinelCredential) {
					t.Fatalf("credential leaked from returned error: %q", err)
				}
				if errors.Unwrap(err) != nil {
					t.Fatalf("transport error retained an unsafe wrapped cause: %T", errors.Unwrap(err))
				}
				var deliveryErr *DeliveryError
				if !errors.As(err, &deliveryErr) {
					t.Fatalf("error type = %T, want *DeliveryError", err)
				}
				if deliveryErr.Provider != provider || deliveryErr.Stage != "send" || deliveryErr.Cause != cause.want {
					t.Fatalf("delivery error = %#v", deliveryErr)
				}
			})
		}
	}
}

func TestChannelRequestErrorsNeverRetainMalformedCredentialURL(t *testing.T) {
	channels := []Channel{
		NewDiscord("discord-main", "://"+sentinelCredential, time.Second),
		NewTelegram("telegram-main", "://invalid", sentinelCredential, "123", time.Second),
		NewSignal("signal-main", "://"+sentinelCredential, "+15550001111", "recipient", time.Second),
	}

	for _, channel := range channels {
		t.Run(channel.Type(), func(t *testing.T) {
			err := channel.Send(context.Background(), canonical.Message{Body: "test"})
			if err == nil {
				t.Fatal("expected request construction error")
			}
			if strings.Contains(err.Error(), sentinelCredential) {
				t.Fatalf("credential leaked from request error: %q", err)
			}
			var deliveryErr *DeliveryError
			if !errors.As(err, &deliveryErr) {
				t.Fatalf("error type = %T, want *DeliveryError", err)
			}
			if deliveryErr.Provider != channel.Type() || deliveryErr.Stage != "request" || deliveryErr.Cause != "invalid_request" {
				t.Fatalf("delivery error = %#v", deliveryErr)
			}
		})
	}
}

func TestSafeErrorBoundsTypedFields(t *testing.T) {
	got := SafeError(&DeliveryError{
		Provider: sentinelCredential,
		Stage:    sentinelCredential,
		Status:   999,
		Cause:    sentinelCredential,
	})

	if strings.Contains(got, sentinelCredential) || strings.Contains(got, "999") {
		t.Fatalf("typed fields bypassed safe rendering: %q", got)
	}
	if got != "provider=unknown stage=unknown cause=unknown" {
		t.Fatalf("SafeError() = %q", got)
	}
}
