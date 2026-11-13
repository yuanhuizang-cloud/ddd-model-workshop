package booking

import (
	"errors"
	"sync"
	"testing"
)

func setup(t *testing.T) *Store {
	t.Helper()
	v, err := NewVoyage("V001", 10)
	if err != nil {
		t.Fatal(err)
	}
	return NewStore(v)
}
func TestInvariants(t *testing.T) {
	for _, slots := range []int{-1, 0, 11} {
		v, _ := NewVoyage("v", 10)
		if !errors.Is(v.Reserve(slots), ErrRule) {
			t.Fatal(slots)
		}
	}
	v, _ := NewVoyage("v", 10)
	if v.Reserve(10) != nil || v.Reserved() != 10 {
		t.Fatal(v)
	}
}
func TestReplayAndMismatch(t *testing.T) {
	s := setup(t)
	cmd := Command{"a", "V001", 10}
	a, err := Book(s, cmd)
	if err != nil {
		t.Fatal(err)
	}
	b, err := Book(s, cmd)
	if err != nil || a != b || len(s.Events()) != 1 {
		t.Fatal(a, b, err)
	}
	_, err = Book(s, Command{"a", "V001", 3})
	if !errors.Is(err, ErrKeyReuse) {
		t.Fatal(err)
	}
}
func TestConflict(t *testing.T) {
	s := setup(t)
	a, _ := s.Load("V001")
	b, _ := s.Load("V001")
	a.Reserve(6)
	b.Reserve(6)
	if _, err := s.Commit(a, 0, Command{"a", "V001", 6}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Commit(b, 0, Command{"b", "V001", 6}); !errors.Is(err, ErrConflict) {
		t.Fatal(err)
	}
}
func TestConcurrentBooking(t *testing.T) {
	s := setup(t)
	var wg sync.WaitGroup
	result := make(chan error, 2)
	for _, id := range []string{"a", "b"} {
		wg.Add(1)
		go func(id string) { defer wg.Done(); _, err := Book(s, Command{id, "V001", 6}); result <- err }(id)
	}
	wg.Wait()
	close(result)
	successes := 0
	for err := range result {
		if err == nil {
			successes++
		} else if !errors.Is(err, ErrRule) {
			t.Fatal(err)
		}
	}
	v, _ := s.Load("V001")
	if successes != 1 || v.Reserved() != 6 || len(s.Events()) != 1 {
		t.Fatal(successes, v)
	}
}
func TestConcurrentDuplicate(t *testing.T) {
	s := setup(t)
	var wg sync.WaitGroup
	result := make(chan error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _, err := Book(s, Command{"same", "V001", 10}); result <- err }()
	}
	wg.Wait()
	close(result)
	for err := range result {
		if err != nil {
			t.Fatal(err)
		}
	}
	if len(s.Events()) != 1 {
		t.Fatal(s.Events())
	}
}
