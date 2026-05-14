from sqlalchemy import Column, Integer, String, Text, DateTime, Numeric, func
from sqlalchemy.dialects.postgresql import JSONB
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
    order_id = Column(String, index=True, nullable=True) # Unique ID for the checkout session
    user_id = Column(String, nullable=False)            # session ID
    customer_name = Column(String, nullable=False)
    customer_email = Column(String, nullable=False)
    product_id = Column(String, nullable=False)         # Barcode or ItemCode
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, default=1)
    address = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

class Product(Base):
    __tablename__ = "products"

    barcode = Column(String, primary_key=True, index=True) # Master Linking Key
    item_code = Column(String, unique=True, index=True, nullable=True)
    item_name = Column(Text, index=True, nullable=False)
    brand = Column(String, index=True, nullable=True)
    category = Column(String, index=True, nullable=True)
    description = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    
    # AI Intelligence Fields
    skin_type = Column(String, nullable=True)
    concerns = Column(JSONB, nullable=True)    # Changed to JSONB for GIN Index
    tags = Column(JSONB, nullable=True)        # Changed to JSONB for GIN Index
    
    # Commerce Data (Synced from SAP)
    price = Column(Numeric(12, 2), default=0.00)
    available_qty = Column(Integer, default=0)
    
    # Metadata
    last_synced_sap = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())