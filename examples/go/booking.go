// Package booking 演示领域聚合和内存事务适配器；无持久性保证。
package booking

import (
	"errors"
	"sync"
)

var ErrRule = errors.New("领域规则不满足")
var ErrConflict = errors.New("版本冲突")
var ErrKeyReuse = errors.New("幂等键参数不同")

type Voyage struct {
	id                          string
	capacity, reserved, version int
}

func NewVoyage(id string, capacity int) (Voyage, error) {
	if id == "" || capacity < 0 {
		return Voyage{}, ErrRule
	}
	return Voyage{id: id, capacity: capacity}, nil
}
func (v *Voyage) Reserve(slots int) error {
	if slots <= 0 || slots > v.capacity-v.reserved {
		return ErrRule
	}
	v.reserved += slots
	return nil
}
func (v Voyage) Reserved() int { return v.reserved }

type Command struct {
	ID, VoyageID string
	Slots        int
}
type Event struct {
	ID, VoyageID             string
	Slots, Reserved, Version int
}
type receipt struct {
	command Command
	result  Event
}

// Store 的互斥锁仅保护提交边界；领域决策针对快照进行。
// 生产环境应把版本条件更新、幂等记录、outbox 放在数据库同一事务中。
type Store struct {
	mu       sync.Mutex
	voyages  map[string]Voyage
	receipts map[string]receipt
	outbox   []Event
}

func NewStore(v Voyage) *Store {
	return &Store{voyages: map[string]Voyage{v.id: v}, receipts: map[string]receipt{}}
}
func (s *Store) Load(id string) (Voyage, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.voyages[id]
	if !ok {
		return Voyage{}, ErrRule
	}
	return v, nil
}
func (s *Store) replay(cmd Command) (Event, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.receipts[cmd.ID]
	if !ok {
		return Event{}, false, nil
	}
	if r.command != cmd {
		return Event{}, true, ErrKeyReuse
	}
	return r.result, true, nil
}
func (s *Store) Commit(v Voyage, expected int, cmd Command) (Event, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if r, ok := s.receipts[cmd.ID]; ok {
		if r.command != cmd {
			return Event{}, ErrKeyReuse
		}
		return r.result, nil
	}
	current, ok := s.voyages[v.id]
	if !ok || current.version != expected {
		return Event{}, ErrConflict
	}
	v.version = expected + 1
	event := Event{cmd.ID, v.id, cmd.Slots, v.reserved, v.version}
	s.voyages[v.id] = v
	s.receipts[cmd.ID] = receipt{cmd, event}
	s.outbox = append(s.outbox, event)
	return event, nil
}
func (s *Store) Events() []Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]Event(nil), s.outbox...)
}

// Book 是应用服务：协调重放、加载、领域行为与提交。
func Book(s *Store, cmd Command) (Event, error) {
	if cmd.ID == "" || cmd.VoyageID == "" || cmd.Slots <= 0 {
		return Event{}, ErrRule
	}
	for attempt := 0; attempt < 5; attempt++ {
		if event, found, err := s.replay(cmd); found {
			return event, err
		}
		v, err := s.Load(cmd.VoyageID)
		if err != nil {
			return Event{}, err
		}
		expected := v.version
		if err = v.Reserve(cmd.Slots); err != nil {
			if event, found, replayErr := s.replay(cmd); found {
				return event, replayErr
			}
			return Event{}, err
		}
		event, err := s.Commit(v, expected, cmd)
		if errors.Is(err, ErrConflict) {
			continue
		}
		return event, err
	}
	return Event{}, ErrConflict
}
