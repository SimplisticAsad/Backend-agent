CREATE TABLE departments (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  name text NOT NULL,
  description text,
  CONSTRAINT pk_departments PRIMARY KEY (id)
);
CREATE TABLE users (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  email text NOT NULL,
  password_hash text NOT NULL,
  full_name text NOT NULL,
  role text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_users PRIMARY KEY (id),
  CONSTRAINT uq_users_email UNIQUE (email),
  CONSTRAINT ck_users_role CHECK (role IN ('manager', 'employee'))
);
CREATE TABLE employees (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  user_id uuid NOT NULL,
  department_id uuid,
  job_title text,
  CONSTRAINT pk_employees PRIMARY KEY (id),
  CONSTRAINT fk_employees_user_id FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT fk_employees_department_id FOREIGN KEY (department_id) REFERENCES departments (id) ON DELETE RESTRICT
);
CREATE INDEX ix_employees_user_id ON employees (user_id);
CREATE INDEX ix_employees_department_id ON employees (department_id);
CREATE TABLE projects (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  name text NOT NULL,
  description text,
  status text NOT NULL,
  owner_id uuid,
  due_date date,
  CONSTRAINT pk_projects PRIMARY KEY (id),
  CONSTRAINT fk_projects_owner_id FOREIGN KEY (owner_id) REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT ck_projects_status CHECK (status IN ('planned', 'active', 'completed'))
);
CREATE INDEX ix_projects_owner_id ON projects (owner_id);
CREATE TABLE project_members (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  project_id uuid NOT NULL,
  user_id uuid NOT NULL,
  CONSTRAINT pk_project_members PRIMARY KEY (id),
  CONSTRAINT fk_project_members_project_id FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE,
  CONSTRAINT fk_project_members_user_id FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE INDEX ix_project_members_project_id ON project_members (project_id);
CREATE INDEX ix_project_members_user_id ON project_members (user_id);
CREATE TABLE tasks (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  title text NOT NULL,
  description text,
  status text NOT NULL,
  due_date date,
  project_id uuid NOT NULL,
  assignee_id uuid,
  CONSTRAINT pk_tasks PRIMARY KEY (id),
  CONSTRAINT fk_tasks_project_id FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE RESTRICT,
  CONSTRAINT fk_tasks_assignee_id FOREIGN KEY (assignee_id) REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT ck_tasks_status CHECK (status IN ('todo', 'in_progress', 'completed'))
);
CREATE INDEX ix_tasks_project_id ON tasks (project_id);
CREATE INDEX ix_tasks_assignee_id ON tasks (assignee_id);
