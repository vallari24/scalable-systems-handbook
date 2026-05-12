# Airline Check-In System: Transactions, Indexes, and Locks

Imagine an airline check-in system where many passengers are selecting seats at the same time.

The risky case:

1. Passenger A opens seat `6A`.
2. Passenger B opens seat `6A`.
3. Both see the seat as available.
4. Both try to reserve it.

The database has to do two different jobs well:

![Mental model](../assets/airline-checkin-locking/mental-model.svg)

- **Find the row quickly:** use an index.
- **Change the row safely:** use a transaction and a lock.

## Transaction

A transaction wraps multiple database operations so they behave like one logical operation.

For check-in, reserving a seat may involve several writes:

![Transaction boundary](../assets/airline-checkin-locking/transaction-boundary.svg)

```sql
BEGIN;

SELECT *
FROM seats
WHERE flight_id = 42
  AND seat_number = '6A'
FOR UPDATE;

UPDATE seats
SET status = 'reserved',
    passenger_id = 1001
WHERE flight_id = 42
  AND seat_number = '6A'
  AND status = 'available';

UPDATE bookings
SET checked_in_at = CURRENT_TIMESTAMP
WHERE id = 5001;

COMMIT;
```

If the seat update succeeds but the booking update fails, the database should not leave the system half-finished. That is the point of the transaction boundary.

## ACID Properties

ACID is the set of promises a relational database tries to provide around a transaction.

![ACID properties](../assets/airline-checkin-locking/acid-properties.svg)

**Atomicity:** all-or-nothing.  
Either the seat is reserved and the booking is checked in, or neither change is kept.

**Consistency:** valid state to valid state.  
One seat cannot belong to two passengers. A checked-in booking must belong to a real passenger. A seat cannot be both `available` and `reserved`.

**Isolation:** concurrent transactions should not corrupt each other.  
If two passengers race for seat `6A`, isolation prevents both transactions from acting as if they were alone.

**Durability:** committed data survives failures.  
Once the database commits passenger A to seat `6A`, that assignment should survive a database restart.

## Why Indexes Make Reads Faster

Before the database can lock or update seat `6A`, it has to find the row.

The hot lookup is:

```sql
SELECT *
FROM seats
WHERE flight_id = 42
  AND seat_number = '6A';
```

Without a useful index, the database may scan many table blocks to find one row.

With this index:

```sql
CREATE INDEX idx_seats_flight_seat
ON seats(flight_id, seat_number);
```

the database gets a smaller ordered structure for lookup.

![Index lookup](../assets/airline-checkin-locking/index-lookup.svg)

The fast path becomes:

1. search the index for `(42, '6A')`
2. get the row id
3. fetch the matching row from the table

That is why indexes make reads faster: they replace broad table scans with targeted row fetches.

Indexes are not free. Every insert, update, or delete must also maintain the index. So an index is a write tax you choose because a specific read path matters.

For airline check-in, `(flight_id, seat_number)` is a good index because the product constantly asks: "for this flight, what is the state of this seat?"

## Why Locks Are Needed

Indexes make the row easy to find. Locks make the row safe to change.

Without a lock, two transactions can both read the same old state:

![Lost update](../assets/airline-checkin-locking/lost-update.svg)

Both transactions saw `available = 1`. Both wrote a reservation. Now two passengers may believe they own seat `6A`.

Locks protect the sanity of the data:

- **consistency:** the database remains valid
- **integrity:** seat assignment rules are not broken

The rule here is simple: one seat can have only one confirmed passenger.

## Pessimistic Locking

Pessimistic locking means the database assumes conflict can happen, so the transaction takes the lock before entering the critical section.

![Pessimistic locking flow](../assets/airline-checkin-locking/pessimistic-lock-flow.svg)

The shape is:

```text
ACQ_LOCK()
READ / UPDATE critical section
REL_LOCK()
```

Only one incompatible transaction can own the same row at the same time. The other transaction waits, skips the row, or fails immediately depending on the query.

![Lock behavior map](../assets/airline-checkin-locking/lock-behavior-map.svg)

## Shared Locks

A shared lock is a read lock.

Other transactions can read the locked rows, but they cannot modify those rows until the lock is released.

```sql
SELECT *
FROM seats
WHERE seat_id IN (1, 2, 6)
FOR SHARE;
```

![Shared locks](../assets/airline-checkin-locking/shared-locks.svg)

Transaction 1 takes shared locks on seats `1`, `2`, and `6`.

Transaction 2 can still read seat `6`, because shared locks are compatible with other shared locks.

But if transaction 2 tries to update seat `6`, it waits. A write needs an exclusive lock.

## Exclusive Locks

An exclusive lock is a write lock.

Other transactions cannot modify the locked rows. Locking reads that need those rows also wait.

```sql
SELECT *
FROM seats
WHERE seat_id IN (1, 2, 6)
FOR UPDATE;
```

![Exclusive locks](../assets/airline-checkin-locking/exclusive-locks.svg)

Transaction 1 locks seats `1`, `2`, and `6` for update.

Transaction 2 asks for seats `3`, `4`, and `6`. Seats `3` and `4` are free, but seat `6` is already locked, so transaction 2 waits.

This protects correctness, but it lowers throughput when many transactions fight for the same rows.

## Deadlocks

A deadlock is a waiting cycle.

![Deadlock cycle](../assets/airline-checkin-locking/deadlock-cycle.svg)

Example:

```text
T1 locks seat 1
T2 locks seat 2

T1 now wants seat 2
T2 now wants seat 1
```

Neither transaction can continue.

Relational databases detect this cycle and abort one transaction. The aborted transaction releases its locks, the other transaction continues, and the application can retry the aborted work when safe.

## `SKIP LOCKED`

Sometimes waiting is not useful.

For a standby-seat worker, it may be fine to skip a locked seat and take another available seat.

```sql
SELECT *
FROM seats
WHERE seat_id IN (3, 4, 6)
FOR UPDATE SKIP LOCKED;
```

![Skip locked](../assets/airline-checkin-locking/skip-locked.svg)

The result set contains only seats `3` and `4`.

Seat `6` is locked, so the database removes it from the result set instead of making the transaction wait.

## `NOWAIT`

Sometimes waiting is wrong, but skipping is also wrong.

If a passenger explicitly selected seat `6A`, the application may want fast failure instead of blocking.

```sql
SELECT *
FROM seats
WHERE seat_id = 6
FOR UPDATE NOWAIT;
```

![Nowait](../assets/airline-checkin-locking/nowait.svg)

`NOWAIT` means:

```text
Try to acquire the lock.
If the row is already locked, fail immediately.
Do not wait.
```

The app can then refresh the seat map or ask the passenger to choose another seat.

## The Tradeoff

Indexes and locks solve different problems:

```text
index -> find the row fast
lock  -> change the row safely
```

Locking protects data sanity, but it is not free:

```text
more locking  -> safer critical sections, lower concurrency
less locking  -> higher concurrency, more conflict risk
```

For airline check-in, correctness matters more than raw speed on the final seat reservation step. It is better to make one passenger wait briefly than to double-book the same seat.
