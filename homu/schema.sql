CREATE TABLE IF NOT EXISTS pull (
    id INT NOT NULL AUTO_INCREMENT,
    repo VARCHAR(255) NOT NULL,
    num INTEGER NOT NULL,
    status TEXT NOT NULL,
    merge_sha TEXT,
    title TEXT,
    body TEXT,
    head_sha TEXT,
    head_ref TEXT,
    base_ref TEXT,
    assignee TEXT,
    approved_by TEXT,
    priority INTEGER,
    try_ INTEGER,
    rollup INTEGER,
    PRIMARY KEY (id),
    UNIQUE unique_index (repo, num));

CREATE TABLE IF NOT EXISTS build_res (
    id INT NOT NULL AUTO_INCREMENT,
    repo VARCHAR(255) NOT NULL,
    num INTEGER NOT NULL,
    builder VARCHAR(255) NOT NULL,
    res INTEGER,
    url TEXT NOT NULL,
    merge_sha TEXT NOT NULL,
    PRIMARY KEY (id),
    UNIQUE unique_index (repo, num, builder));

CREATE TABLE IF NOT EXISTS mergeable (
    id INT NOT NULL AUTO_INCREMENT,
    repo VARCHAR(255) NOT NULL,
    num INTEGER NOT NULL,
    mergeable INTEGER NOT NULL,
    PRIMARY KEY (id),
    UNIQUE unique_index (repo, num));
