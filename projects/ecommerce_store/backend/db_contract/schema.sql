CREATE TABLE categories (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  name text NOT NULL,
  CONSTRAINT pk_categories PRIMARY KEY (id)
);
CREATE TABLE products (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  name text NOT NULL,
  description text,
  price numeric(12,2) NOT NULL,
  stock integer NOT NULL,
  category_id uuid,
  image_url text,
  CONSTRAINT pk_products PRIMARY KEY (id),
  CONSTRAINT fk_products_category_id FOREIGN KEY (category_id) REFERENCES categories (id) ON DELETE RESTRICT,
  CONSTRAINT ck_products_price CHECK (price >= 0),
  CONSTRAINT ck_products_stock CHECK (stock >= 0)
);
CREATE INDEX ix_products_category_id ON products (category_id);
CREATE TABLE users (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  email text NOT NULL,
  password_hash text NOT NULL,
  full_name text NOT NULL,
  role text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_users PRIMARY KEY (id),
  CONSTRAINT uq_users_email UNIQUE (email),
  CONSTRAINT ck_users_role CHECK (role IN ('customer'))
);
CREATE TABLE cart_items (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  user_id uuid NOT NULL,
  product_id uuid NOT NULL,
  quantity integer NOT NULL,
  CONSTRAINT pk_cart_items PRIMARY KEY (id),
  CONSTRAINT fk_cart_items_user_id FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT fk_cart_items_product_id FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE RESTRICT,
  CONSTRAINT ck_cart_items_quantity CHECK (quantity > 0)
);
CREATE INDEX ix_cart_items_user_id ON cart_items (user_id);
CREATE INDEX ix_cart_items_product_id ON cart_items (product_id);
CREATE TABLE orders (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  user_id uuid NOT NULL,
  status text NOT NULL,
  total numeric(12,2) NOT NULL,
  shipping_address text,
  placed_at timestamptz DEFAULT now(),
  CONSTRAINT pk_orders PRIMARY KEY (id),
  CONSTRAINT fk_orders_user_id FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
  CONSTRAINT ck_orders_status CHECK (status IN ('pending', 'paid', 'shipped', 'delivered', 'cancelled')),
  CONSTRAINT ck_orders_total CHECK (total >= 0)
);
CREATE INDEX ix_orders_user_id ON orders (user_id);
CREATE TABLE order_items (
  id uuid DEFAULT gen_random_uuid() NOT NULL,
  order_id uuid NOT NULL,
  product_id uuid NOT NULL,
  quantity integer NOT NULL,
  unit_price numeric(12,2) NOT NULL,
  CONSTRAINT pk_order_items PRIMARY KEY (id),
  CONSTRAINT fk_order_items_order_id FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE,
  CONSTRAINT fk_order_items_product_id FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE RESTRICT,
  CONSTRAINT ck_order_items_quantity CHECK (quantity > 0),
  CONSTRAINT ck_order_items_unit_price CHECK (unit_price >= 0)
);
CREATE INDEX ix_order_items_order_id ON order_items (order_id);
CREATE INDEX ix_order_items_product_id ON order_items (product_id);
