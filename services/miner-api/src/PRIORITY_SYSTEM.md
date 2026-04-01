# Request Priority System for Mining Proxy

## Overview

The priority system enables intelligent request management for optimal GPU utilization. It ensures external (real) requests always get priority over dummy requests while maintaining minimum GPU concurrency for efficiency.

## Key Features

### 1. **Dynamic Request Abortion**
- When an external request arrives and the system is at capacity, the priority manager automatically aborts a dummy request to make room
- Abortion is selective: newer dummy requests are aborted first (LIFO strategy)
- Batch-aware: Aborts individual requests within a batch, not entire batches

### 2. **Concurrency Management**
- Maintains minimum concurrent requests (configurable via `MIN_ACTIVE_REQUESTS`)
- Sets maximum concurrent requests to prevent overload
- Automatically generates dummy requests when below minimum threshold

### 3. **Batch-Aware Dummy Generation**
- Generates dummy requests individually rather than as a single batched request
- Each dummy can be independently aborted without affecting others
- Supports position tracking within batches for intelligent abortion

## Architecture

### Components

1. **RequestPriorityManager** (`request_priority_manager.py`)
   - Core priority logic
   - Tracks all active requests
   - Handles abortion decisions
   - Maintains statistics

2. **PriorityRequestManager** (`proxy_with_priority.py`)
   - Enhanced proxy with priority support
   - Integrates priority manager with existing proxy
   - Handles batch generation with cancellation support

## How It Works

### Request Flow

1. **External Request Arrives**:
   ```
   External Request → Register with Priority Manager
                   → Check Capacity
                   → If full: Abort a dummy request
                   → Process request
                   → Unregister when complete
   ```

2. **Dummy Request Generation**:
   ```
   Monitor Loop → Check if below minimum
               → Generate batch of individual dummies
               → Each dummy registers with priority manager
               → Can be aborted at any time
   ```

### Abortion Strategy

The system uses a multi-factor priority for choosing which dummy to abort:

1. **Age**: Newer requests are aborted first (LIFO)
2. **Batch Position**: Higher positions in batch aborted first
3. **Safety Check**: Won't abort if it drops below minimum concurrency

### Example Scenario

```
Current State: 4 active requests (min=4, max=8)
- 1 external request
- 3 dummy requests

New external request arrives:
1. System at minimum (4 requests)
2. Can accommodate without abortion (4 < 8)
3. External request proceeds
4. Total: 5 requests (2 external, 3 dummy)

Another external arrives (now at 5):
1. Still below max (5 < 8)
2. Proceeds without abortion
3. Total: 6 requests (3 external, 3 dummy)

When at capacity (8 requests):
1. New external arrives
2. System finds newest dummy request
3. Aborts dummy request
4. External proceeds
5. Still at 8 requests (but now more external)
```

## Configuration

### Environment Variables

```bash
# Minimum concurrent requests to maintain
MIN_ACTIVE_REQUESTS=4

# Maximum concurrent requests (typically 2x minimum)
# Not directly configurable, computed as MIN_ACTIVE_REQUESTS * 2

# Batch size for dummy generation
BATCH_SIZE=20

# Request timeouts
DUMMY_REQUEST_TIMEOUT=30
DUMMY_RETRY_ATTEMPTS=10
DUMMY_RETRY_BACKOFF=1.0
```

### Enabling Priority System

To enable the priority system, modify `main.py`:

```python
# Replace the standard RequestManager import
# from components.proxy import RequestManager
from components.proxy_with_priority import PriorityRequestManager

# In MiningProxyApp.__init__:
self.request_manager = PriorityRequestManager(self.context)
```

## API Endpoints

### Enhanced /status Endpoint

With priority system enabled, the `/status` endpoint includes additional information:

```json
{
  "proxy": {
    "active_requests": 6,
    "min_active": 4,
    "requests_by_type": {
      "real": 2,
      "dummy": 4
    }
  },
  "priority": {
    "total_external": 150,
    "total_dummy": 500,
    "total_aborted": 45,
    "current_external": 2,
    "current_dummy": 4,
    "capacity_used": 0.75,
    "can_accept_external": true
  }
}
```

## Monitoring

### Logs

The priority system provides detailed logging:

```
INFO: External request ext-abc123 started
INFO: Aborted dummy request dummy-xyz789 (age=2.3s, batch_pos=15) to make room
INFO: Generating dummy batch batch-def456 with 20 requests
DEBUG: Stopping batch generation at position 10: minimum concurrency reached
```

### Metrics to Monitor

1. **Abortion Rate**: `total_aborted / total_dummy`
   - High rate indicates heavy external load
   - Target: < 20%

2. **Capacity Utilization**: `active_requests / max_concurrent`
   - Should stay between 50-90%
   - Too low: wasted GPU capacity
   - Too high: may reject external requests

3. **External vs Dummy Ratio**: `current_external / current_dummy`
   - Indicates real workload
   - Dummy requests should decrease as external load increases

## Performance Considerations

### Benefits

1. **Improved Response Time**: External requests never wait for dummy requests
2. **Better GPU Utilization**: Maintains optimal concurrency
3. **Reduced Waste**: Aborts only what's necessary
4. **Scalable**: Handles varying external load gracefully

### Trade-offs

1. **Complexity**: More complex than simple FIFO queue
2. **Overhead**: Small overhead for tracking and decision making
3. **Aborted Work**: Some GPU cycles wasted on aborted dummy requests

### Tuning Guidelines

1. **For High External Load**:
   - Decrease `MIN_ACTIVE_REQUESTS` to reduce dummy generation
   - Increase `DUMMY_REQUEST_TIMEOUT` to reduce retries

2. **For Low External Load**:
   - Increase `MIN_ACTIVE_REQUESTS` for better GPU utilization
   - Decrease `BATCH_SIZE` for more responsive abortion

3. **For Variable Load**:
   - Keep defaults but monitor abortion rate
   - Adjust based on patterns

## Testing

### Unit Tests

```bash
# Test priority manager
python -m pytest tests/test_priority_manager.py

# Test integrated proxy
python -m pytest tests/test_proxy_with_priority.py
```

### Load Testing

```bash
# Simulate mixed load
python tests/load_test_priority.py \
  --external-rate 10 \
  --external-burst 50 \
  --duration 300
```

### Scenarios to Test

1. **Burst Protection**: Send 100 external requests rapidly
2. **Steady State**: Maintain 80% external, 20% dummy mix
3. **Recovery**: After burst, verify dummy generation resumes
4. **Edge Cases**: Test at exactly min/max boundaries

## Troubleshooting

### Issue: External requests being rejected

**Symptoms**: 503 errors for external requests

**Solutions**:
1. Increase max concurrent (modify multiplier in code)
2. Decrease batch size
3. Check if dummies are marked as abortable

### Issue: GPU underutilized

**Symptoms**: Active requests consistently below minimum

**Solutions**:
1. Check VDF and miner initialization
2. Verify dummy generation is working
3. Check for errors in dummy request execution

### Issue: High abortion rate

**Symptoms**: > 30% of dummies being aborted

**Solutions**:
1. Decrease MIN_ACTIVE_REQUESTS
2. Increase time between dummy generation
3. Consider increasing max concurrent limit

## Future Enhancements

1. **Predictive Abortion**: Use ML to predict external request patterns
2. **Quality of Service**: Different priority levels for external requests
3. **Cost-Based Abortion**: Consider request progress before aborting
4. **Dynamic Limits**: Auto-adjust min/max based on load patterns
5. **Request Pooling**: Pre-warmed request pool for faster response