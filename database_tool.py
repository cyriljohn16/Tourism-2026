#!/usr/bin/env python
"""
Database Management Tool for Tourism Project
Allows direct editing of MariaDB without using mysql command line
"""
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tourism_project.settings')
import django
django.setup()

import MySQLdb
from django.contrib.auth.hashers import make_password

def connect_db():
    return MySQLdb.connect(
        host='127.0.0.1',
        port=3307,
        user='root',
        passwd='september242023',
        db='project_db'
    )

def show_employees():
    """Display all employees"""
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("SELECT emp_id, first_name, last_name, email, role, status FROM admin_app_employee ORDER BY emp_id")
    print("\n📋 ALL EMPLOYEES:")
    for emp in cursor.fetchall():
        print(f"   ID:{emp[0]:2} | {emp[1]} {emp[2]:15} | {emp[3]:30} | Role:{emp[4]:8} | Status:{emp[5]}")
    conn.close()

def show_tables():
    """Display all tables in the database"""
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("SHOW TABLES")
    print("\n📋 ALL TABLES:")
    tables = cursor.fetchall()
    for table in tables:
        print(f"   {table[0]}")
    conn.close()

def show_accommodations():
    """Display all accommodations"""
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("SELECT accom_id, company_name, email_address, company_type, status FROM admin_app_accomodation ORDER BY accom_id")
    print("\n🏨 ALL ACCOMMODATIONS:")
    records = cursor.fetchall()
    if records:
        for acc in records:
            print(f"   ID:{acc[0]:2} | {acc[1]:30} | {acc[2]:30} | Type:{acc[3]:10} | Status:{acc[4]}")
    else:
        print("   (None yet)")
    conn.close()

def create_employee(email, password, first_name, last_name, username, age, phone, sex='M'):
    """Create a new employee"""
    conn = connect_db()
    cursor = conn.cursor()
    try:
        hashed_pwd = make_password(password)
        cursor.execute("""
            INSERT INTO admin_app_employee 
            (first_name, last_name, username, age, phone_number, email, sex, role, status, is_active, is_staff, is_superuser, password)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (first_name, last_name, username, age, phone, email, sex, 'employee', 'accepted', True, False, False, hashed_pwd))
        conn.commit()
        print(f"✓ Employee created: {email}")
        return True
    except Exception as e:
        print(f"✗ Error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def create_accommodation(company_name, email, location, company_type, phone, password):
    """Create a new accommodation account"""
    conn = connect_db()
    cursor = conn.cursor()
    try:
        hashed_pwd = make_password(password)
        cursor.execute("""
            INSERT INTO admin_app_accomodation 
            (company_name, email_address, location, company_type, phone_number, status, is_active, is_staff, is_superuser, password)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (company_name, email, location, company_type, phone, 'accepted', True, False, False, hashed_pwd))
        conn.commit()
        print(f"✓ Accommodation created: {email}")
        return True
    except Exception as e:
        print(f"✗ Error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def update_employee_status(email, status):
    """Update employee status"""
    conn = connect_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE admin_app_employee SET status = %s WHERE email = %s", (status, email))
        conn.commit()
        print(f"✓ Updated {email} status to: {status}")
        return True
    except Exception as e:
        print(f"✗ Error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def promote_to_admin(email):
    """Promote employee to admin"""
    conn = connect_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE admin_app_employee SET role = %s WHERE email = %s", ('admin', email))
        conn.commit()
        print(f"✓ Promoted {email} to admin role")
        return True
    except Exception as e:
        print(f"✗ Error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def delete_employee(email):
    """Delete an employee"""
    conn = connect_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM admin_app_employee WHERE email = %s", (email,))
        conn.commit()
        print(f"✓ Deleted employee: {email}")
        return True
    except Exception as e:
        print(f"✗ Error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def create_tour_schedule_table():
    """Create the missing tour_app_tour_schedule table"""
    conn = connect_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE tour_app_tour_schedule (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                sched_id VARCHAR(10) NOT NULL UNIQUE,
                tour_id VARCHAR(7) NOT NULL,
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                price DECIMAL(10,2) NOT NULL,
                slots_available INT UNSIGNED NOT NULL DEFAULT 0,
                slots_booked INT UNSIGNED NOT NULL DEFAULT 0,
                duration_days INT UNSIGNED NOT NULL DEFAULT 1,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                cancellation_reason TEXT,
                cancellation_date DATETIME NULL,
                FOREIGN KEY (tour_id) REFERENCES tour_app_tour_add(tour_id)
            )
        """)
        conn.commit()
        print("✓ Created tour_app_tour_schedule table")
        return True
    except Exception as e:
        print(f"✗ Error creating table: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

# RUN EXAMPLES
if __name__ == "__main__":
    print("=" * 80)
    print("DATABASE MANAGEMENT TOOL - EXAMPLES")
    print("=" * 80)
    
    # Show current employees
    show_employees()
    show_accommodations()
    show_tables()

    # Create missing table
    print("\n▶ CREATING MISSING TOUR SCHEDULE TABLE...")
    create_tour_schedule_table()
    
    # Create sample employee
    print("\n▶ CREATING NEW EMPLOYEE...")
    create_employee(
        email='jane@example.com',
        password='Jane@456',
        first_name='Jane',
        last_name='Smith',
        username='jane_smith',
        age=25,
        phone='09112223334'
    )
    
    # Promote to admin
    print("▶ PROMOTING TO ADMIN...")
    promote_to_admin('jane@example.com')
    
    # Create accommodation
    print("\n▶ CREATING ACCOMMODATION...")
    create_accommodation(
        company_name='Ocean View Resort',
        email='resort@example.com',
        location='Bayawan Beach',
        company_type='hotel',
        phone='09223334445',
        password='Resort@789'
    )
    
    # Show updated data
    print("\n" + "=" * 80)
    print("UPDATED DATABASE STATE:")
    print("=" * 80)
    show_employees()
    show_accommodations()
    
    print("\n" + "=" * 80)
    print("✓ Database operations completed!")
    print("=" * 80)
