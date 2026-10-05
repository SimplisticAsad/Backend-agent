CREATE TABLE users (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  email text NOT NULL,
  password_hash text NOT NULL,
  full_name text NOT NULL,
  role text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_users PRIMARY KEY (id),
  CONSTRAINT uq_users_email UNIQUE (email),
  CONSTRAINT ck_users_role CHECK (role IN ('customer', 'agent', 'manager'))
);
CREATE TABLE tickets (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  title text NOT NULL,
  description text NOT NULL,
  status text NOT NULL,
  priority text NOT NULL,
  requester_id uuid NOT NULL,
  assignee_id uuid,
  created_at timestamptz DEFAULT now(),
  CONSTRAINT pk_tickets PRIMARY KEY (id),
  CONSTRAINT fk_tickets_requester_id FOREIGN KEY (requester_id) REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT fk_tickets_assignee_id FOREIGN KEY (assignee_id) REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT ck_tickets_status CHECK (status IN ('open', 'in_progress', 'resolved', 'closed')),
  CONSTRAINT ck_tickets_priority CHECK (priority IN ('low', 'medium', 'high', 'urgent'))
);
CREATE INDEX ix_tickets_requester_id ON tickets (requester_id);
CREATE INDEX ix_tickets_assignee_id ON tickets (assignee_id);
CREATE TABLE ticket_comments (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  ticket_id uuid NOT NULL,
  author_id uuid NOT NULL,
  body text NOT NULL,
  created_at timestamptz DEFAULT now(),
  CONSTRAINT pk_ticket_comments PRIMARY KEY (id),
  CONSTRAINT fk_ticket_comments_ticket_id FOREIGN KEY (ticket_id) REFERENCES tickets (id) ON DELETE CASCADE,
  CONSTRAINT fk_ticket_comments_author_id FOREIGN KEY (author_id) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE INDEX ix_ticket_comments_ticket_id ON ticket_comments (ticket_id);
CREATE INDEX ix_ticket_comments_author_id ON ticket_comments (author_id);
