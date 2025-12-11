-- Create tasks table
CREATE TABLE tasks (
    task_id VARCHAR(64) PRIMARY KEY,
    task_family VARCHAR(255) NOT NULL,
    task_data JSONB NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    "user" VARCHAR(255) NOT NULL,
    commit_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    dependency_ids JSONB NOT NULL DEFAULT '[]'
);

-- Create indexes
CREATE INDEX ix_tasks_task_family ON tasks (task_family);
CREATE INDEX ix_tasks_status ON tasks (status);
CREATE INDEX ix_tasks_user ON tasks ("user");
