from sqlalchemy import Column, Integer, String, Text, DateTime, func
from database import Base

class ChatHistory(Base):
    __tablename__ = "chat_history"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String, index=True, nullable=True) # Removed unique constraint to allow multi-item orders
    user_id = Column(String, nullable=False)          # session ID
    customer_name = Column(String, nullable=False)    # new
    customer_email = Column(String, nullable=False)   # new
    product_id = Column(String, nullable=False)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, default=1)
    address = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

class Product(Base):
    __tablename__ = "products"

    item_code = Column(String, primary_key=True, index=True)
    item_name = Column(Text, index=True, nullable=False)
    barcode = Column(String, index=True, nullable=True)
    price = Column(Integer, default=0) 
    available_qty = Column(Integer, default=0)
    category = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    last_updated = Column(DateTime, server_default=func.now(), onupdate=func.now())