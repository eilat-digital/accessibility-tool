# 📊 Monitoring & Logging Guide

## Overview
The accessibility tool now includes comprehensive logging and monitoring capabilities to track system performance, errors, and usage patterns.

## Logging System

### Log Locations

1. **Application Logs**: `logs/app.log`
   - All application events
   - Errors and warnings
   - Processing milestones

2. **SQLite Database Logs**: `db/history.db`
   - Table: `operation_logs`
   - Audit trail of all operations
   - Timestamps and status information

### Log Format

```
2026-03-25 14:30:00,123 - __main__ - INFO - Starting processing for job 550e8400...
2026-03-25 14:30:00,456 - __main__ - INFO - Analyzing PDF pages for job 550e8400...
2026-03-25 14:30:05,789 - __main__ - INFO - Running accessibility script for job 550e8400...
```

### Log Levels

| Level | Usage |
|-------|-------|
| DEBUG | Detailed debugging information |
| INFO | Confirmation that things are working |
| WARNING | Something unexpected happened |
| ERROR | Serious problem, processing failed |
| CRITICAL | System failure |

## Monitoring with /api/stats

Get real-time statistics with the `/api/stats` endpoint:

```bash
$ curl http://localhost:5000/api/stats | jq

{
  "total_documents": 150,
  "successful": 145,
  "failed": 5,
  "success_rate": 96.7,
  "total_pages": 2345,
  "total_size_mb": 45.23,
  "avg_processing_time_seconds": 120.5,
  "documents_today": 12
}
```

### Key Metrics

- **Success Rate**: Percentage of documents processed successfully
- **Average Processing Time**: Mean time to process documents
- **Documents Today**: Real-time count of daily activity
- **Total Capacity**: Pages and size metrics

## Health Checks

Use the `/api/health` endpoint for monitoring:

```bash
$ curl http://localhost:5000/api/health

{
  "status": "ok",
  "timestamp": "2026-03-25T14:35:00",
  "version": "2.0",
  "database": "connected"
}
```

### Monitoring Integration Examples

#### Prometheus Compatible Monitoring

Create a simple monitoring script:

```python
import requests
import time

while True:
    try:
        # Health check
        health = requests.get('http://localhost:5000/api/health').json()
        if health['status'] == 'ok':
            print("✓ System OK")
        
        # Stats monitoring
        stats = requests.get('http://localhost:5000/api/stats').json()
        success_rate = stats['success_rate']
        
        if success_rate < 90:
            print(f"⚠ Low success rate: {success_rate}%")
        
        print(f"Processed: {stats['total_documents']} | "
              f"Success: {stats['success_rate']}% | "
              f"Today: {stats['documents_today']}")
              
    except Exception as e:
        print(f"✗ Error: {e}")
    
    time.sleep(60)
```

#### Bash Monitoring Script

```bash
#!/bin/bash

# Check system every 5 minutes
while true; do
    HEALTH=$(curl -s http://localhost:5000/api/health)
    STATS=$(curl -s http://localhost:5000/api/stats)
    
    # Extract success rate
    SUCCESS_RATE=$(echo $STATS | grep -o '"success_rate": [0-9.]*' | cut -d' ' -f2)
    
    # Alert if success rate is too low
    if (( $(echo "$SUCCESS_RATE < 90" | bc -l) )); then
        echo "ALERT: Success rate is $SUCCESS_RATE%" >&2
        # Send email, Slack message, etc.
    fi
    
    echo "$(date): Health=$HEALTH, Success=$SUCCESS_RATE%"
    sleep 300  # Wait 5 minutes
done
```

## Database Monitoring

### Checking Operation Logs

Access SQLite to review audit trail:

```bash
$ sqlite3 db/history.db ".mode box"

# View operation logs
SELECT * FROM operation_logs ORDER BY timestamp DESC LIMIT 20;

# Count operations by type
SELECT operation, COUNT(*) as count, status FROM operation_logs GROUP BY operation, status;

# Get processing statistics
SELECT AVG(processing_time_seconds), MAX(processing_time_seconds), MIN(processing_time_seconds)
FROM documents WHERE status='done';
```

### Common Queries

```sql
-- Failed documents in last 24 hours
SELECT id, original_name, error, created_at 
FROM documents 
WHERE status='error' 
  AND created_at > datetime('now', '-1 day');

-- Slow processing (over 5 minutes)
SELECT original_name, processing_time_seconds, pages, created_at
FROM documents 
WHERE processing_time_seconds > 300
ORDER BY processing_time_seconds DESC;

-- Daily summary
SELECT 
  date(created_at) as date,
  COUNT(*) as total,
  SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as successful,
  ROUND(100.0 * SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) / COUNT(*), 1) as success_rate,
  SUM(pages) as total_pages,
  ROUND(AVG(processing_time_seconds), 1) as avg_time
FROM documents
GROUP BY date(created_at)
ORDER BY date DESC;
```

## Log File Analysis

### View Recent Errors

```bash
$ grep ERROR logs/app.log | tail -20
```

### Track Processing Times

```bash
$ grep "Processing" logs/app.log | tail -10
```

### Monitor Specific Job

```bash
$ grep "550e8400-e29b-41d4-a716-446655440000" logs/app.log
```

### Get Processing Statistics

```bash
$ grep "processing_time_seconds" logs/app.log | \
  grep -o "Time: [0-9.]*s" | \
  cut -d' ' -f2 | \
  sort -n | \
  tail -20
```

## Performance Optimization

### Identify Bottlenecks

1. Check average processing time from stats
2. Review slow documents in database
3. Monitor OCR performance
4. Track memory usage

### Optimization Tips

- Monitor average processing time
- Alert if success rate drops below 90%
- Track time-of-day patterns
- Identify large files that take longer

## Alerting

### Suggested Alert Thresholds

```
- Success rate < 90%         → Warning
- Success rate < 80%         → Critical
- Avg processing > 5 min     → Warning
- Failed documents in 1h > 5 → Warning
- Database size > 500MB      → Warning
- API response > 5s          → Warning
```

## Troubleshooting

### High Failure Rate
1. Check `logs/app.log` for error patterns
2. Query `documents` table for common error messages
3. Verify PDF file integrity
4. Check system resources

### Slow Processing
1. Monitor `processing_time_seconds` in database
2. Check system CPU and memory
3. Verify OCR services are running
4. Consider reducing DPI or page resolution

### Database Issues
1. Verify database connection with `/api/health`
2. Check disk space
3. Vacuum database: `sqlite3 db/history.db "VACUUM;"`
4. Backup and restore if corrupted

## Regular Maintenance

### Weekly Tasks
- Review error logs
- Check success rates
- Verify backups of `history.db`

### Monthly Tasks
- Analyze processing statistics
- Review performance trends
- Archive old logs
- Database optimization

### Quarterly Tasks
- Full backup of database
- Capacity planning
- Performance tuning
- Security audit

