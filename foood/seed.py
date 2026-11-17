"""The demo world: the Delivery Plus demo restaurants plus the deterministic QA Kitchen.

Names, addresses, coordinates, menus, prices and the sold-out items follow the
Delivery Plus demo seed so the apps look exactly as their authors intended.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from foood.drivers import Driver
from foood.models import MenuCategory, MenuItem, Restaurant
from foood.store import PASSWORD, QA_PASSWORD, Store, owner_email

Item = tuple[str, str, str] | tuple[str, str, str, bool]  # name, description, price[, available]

RESTAURANTS: list[dict[str, Any]] = [
    {
        "name": "Burger Palace",
        "address": "123 Powell St",
        "latitude": 37.7856,
        "longitude": -122.4083,
        "status": "OPEN",
        "cuisine": ["Burgers", "American"],
        "categories": {
            "Mains": [
                ("Classic Burger", "Beef patty, lettuce, tomato, house sauce", "12.99"),
                ("Double Smash", "Two smashed patties, American cheese", "15.49"),
                ("Mushroom Swiss", "Sauteed mushrooms, Swiss cheese", "14.25"),
                ("Crispy Chicken Sandwich", "Buttermilk fried chicken, slaw", "13.50"),
            ],
            "Sides": [
                ("Fries", "Skin-on, sea salt", "3.99"),
                ("Onion Rings", "Beer-battered", "4.49"),
                ("Sweet Potato Fries", "With chipotle mayo", "4.99", False),
            ],
            "Drinks": [
                ("Cola", "330ml can", "1.99"),
                ("Vanilla Shake", "Real vanilla bean ice cream", "5.49"),
            ],
        },
        "uncategorized": [("Chef's Special", "Ask what the grill is doing today", "16.99")],
    },
    {
        "name": "Pizza Napoli",
        "address": "435 Columbus Ave",
        "latitude": 37.7986,
        "longitude": -122.4082,
        "status": "OPEN",
        "cuisine": ["Pizza", "Italian"],
        "categories": {
            "Pizza": [
                ("Margherita", "San Marzano tomato, fior di latte, basil", "11.50"),
                ("Diavola", "Spicy salami, chili oil, mozzarella", "13.75"),
                ("Quattro Formaggi", "Four cheeses", "14.00"),
                ("Truffle Funghi", "Mushrooms, truffle cream", "16.50", False),
            ],
            "Pasta": [
                ("Spaghetti Carbonara", "Guanciale, pecorino, egg", "13.00"),
                ("Penne Arrabbiata", "Tomato, garlic, chili", "10.50"),
            ],
            "Desserts": [("Tiramisu", "Mascarpone, espresso, cocoa", "6.50")],
        },
    },
    {
        "name": "Cairo Kitchen",
        "address": "3251 20th Ave",
        "latitude": 37.728,
        "longitude": -122.4765,
        "status": "OPEN",
        "cuisine": ["Egyptian"],
        "categories": {
            "Mains": [
                ("Koshari", "Rice, lentils, pasta, crispy onions", "7.50"),
                ("Chicken Shawarma Plate", "With garlic sauce and pickles", "11.25"),
                ("Molokhia with Rice", "Slow-cooked jute leaf stew", "9.00"),
            ],
            "Starters": [
                ("Falafel (6 pcs)", "Fava bean ta'ameya with tahini", "4.50"),
                ("Baba Ghanoush", "Smoky eggplant dip with pita", "5.00"),
            ],
            "Desserts": [("Om Ali", "Warm pastry pudding with nuts", "5.75")],
        },
    },
    {
        "name": "Taco Fiesta",
        "address": "3200 Mission St",
        "latitude": 37.745,
        "longitude": -122.42,
        "status": "OPEN",
        "cuisine": ["Mexican"],
        "categories": {
            "Tacos": [
                ("Al Pastor Tacos", "Marinated pork, pineapple", "9.50"),
                ("Baja Fish Tacos", "Beer-battered fish, slaw", "10.75"),
            ],
            "Burritos": [("Carne Asada Burrito", "Grilled steak, rice, beans", "12.25")],
            "Sides": [("Chips & Guacamole", "Made fresh every hour", "5.50")],
        },
    },
    {
        "name": "Sakura Sushi",
        "address": "1581 Webster St",
        "latitude": 37.7852,
        "longitude": -122.4318,
        "status": "BUSY",
        "cuisine": ["Japanese", "Sushi"],
        "categories": {
            "Rolls": [
                ("California Roll", "Crab, avocado, cucumber", "8.50"),
                ("Spicy Tuna Roll", "Tuna, spicy mayo, scallion", "9.75"),
            ],
            "Ramen": [("Tonkotsu Ramen", "Pork broth, chashu, soft egg", "14.50")],
        },
    },
    {
        "name": "Green Bowl",
        "address": "3655 Lawton St",
        "latitude": 37.757,
        "longitude": -122.503,
        "status": "CLOSED",
        "cuisine": ["Healthy", "Salads"],
        "categories": {
            "Bowls": [
                ("Quinoa Power Bowl", "Quinoa, kale, roasted chickpeas", "11.00"),
                ("Caesar Salad", "Romaine, parmesan, croutons", "9.25"),
            ],
        },
    },
    {
        "name": "QA Kitchen",
        "description": "Deterministic restaurant for UI tests",
        "address": "1500 Mission St",
        "latitude": 37.7731,
        "longitude": -122.4167,
        "status": "OPEN",
        "cuisine": ["Test"],
        "owner_email": "qa.restaurant@delivery-plus.test",
        "categories": {
            "Mains": [
                ("QA Burger", "Always $10.00", "10.00"),
                ("QA Pasta", "Always $12.50", "12.50"),
            ],
            "Sides": [("QA Fries", "Always $4.00", "4.00")],
            "Drinks": [("QA Soup", "Always sold out", "5.00", False)],
        },
    },
    {
        "name": "Luigi's Pizzeria",
        "address": "447 Columbus Ave",
        "latitude": 37.799,
        "longitude": -122.4086,
        "status": "OPEN",
        "cuisine": ["Pizza", "Italian"],
        "categories": {
            "Pizza": [
                ("Margherita", "Tomato, mozzarella, basil", "12.00"),
                ("Diavola", "Spicy salami", "14.00"),
            ],
            "Desserts": [("Tiramisu", "The classic", "7.00")],
        },
    },
    {
        "name": "Smash Bros Burgers",
        "address": "130 Powell St",
        "latitude": 37.7858,
        "longitude": -122.408,
        "status": "OPEN",
        "cuisine": ["Burgers"],
        "categories": {
            "Mains": [("Classic Burger", "Smashed patty, pickles, sauce", "13.50")],
            "Sides": [("Fries", "Crinkle cut", "4.25")],
            "Drinks": [("Cola", "330ml can", "1.99")],
        },
    },
    {
        "name": "QA Bistro",
        "description": "Second deterministic restaurant: QA Kitchen's menu at other prices",
        "address": "1521 Mission St",
        "latitude": 37.7735,
        "longitude": -122.4175,
        "status": "OPEN",
        "cuisine": ["Test"],
        "owner_email": "qa.restaurant3@delivery-plus.test",
        "categories": {
            "Mains": [
                ("QA Burger", "Always $11.00", "11.00"),
                ("QA Pasta", "Always $13.00", "13.00"),
            ],
            "Sides": [("QA Fries", "Always $3.50", "3.50")],
            "Drinks": [("QA Soup", "Always $5.00", "5.00")],
        },
    },
    {
        "name": "QA Night Cafe",
        "description": "Closed restaurant for UI tests",
        "address": "30 Van Ness Ave",
        "latitude": 37.7742,
        "longitude": -122.419,
        "status": "CLOSED",
        "cuisine": ["Test"],
        "owner_email": "qa.restaurant2@delivery-plus.test",
        "categories": {"Drinks": [("QA Coffee", "Always $3.00", "3.00")]},
    },
]

#: email -> (vehicle type, licence plate); all verified, offline until they go online in the app
DRIVERS: dict[str, tuple[str, str]] = {
    "driver@example.com": ("Scooter", "DMO-2030"),
    "qa.driver@delivery-plus.test": ("Bicycle", "QA-0001"),
}


def seed_restaurants(store: Store) -> None:
    for spec in RESTAURANTS:
        email = spec.get("owner_email") or owner_email(spec["name"])
        owner = store.user_by_email(email)
        if owner is None:
            password = QA_PASSWORD if email.endswith("@delivery-plus.test") else PASSWORD
            owner = store.add_user(email, password, f"{spec['name']} Owner", "RESTAURANT_OWNER")
        restaurant = Restaurant(
            name=spec["name"],
            owner_id=owner.id,
            address=spec["address"],
            latitude=spec["latitude"],
            longitude=spec["longitude"],
            status=spec["status"],
            description=spec.get("description"),
            cuisine_tags=list(spec.get("cuisine", [])),
        )
        store.restaurants[restaurant.id] = restaurant
        for order, (category_name, items) in enumerate(spec["categories"].items(), start=1):
            category = MenuCategory(restaurant.id, category_name, display_order=order)
            store.categories[category.id] = category
            for item in items:
                _add_item(store, restaurant.id, category.id, item)
        for item in spec.get("uncategorized", []):
            _add_item(store, restaurant.id, None, item)


def _add_item(store: Store, restaurant_id: str, category_id: str | None, item: Item) -> None:
    name, description, price = item[0], item[1], item[2]
    available = item[3] if len(item) > 3 else True
    menu_item = MenuItem(
        restaurant_id=restaurant_id,
        name=name,
        description=description,
        price=Decimal(price),
        category_id=category_id,
        available=bool(available),
    )
    store.menu_items[menu_item.id] = menu_item


def seed_drivers(store: Store) -> None:
    for email, (vehicle_type, plate) in DRIVERS.items():
        user = store.user_by_email(email)
        assert user is not None, email
        driver = Driver(user_id=user.id, vehicle_type=vehicle_type, license_plate=plate)
        driver.verify("VERIFIED", None)
        store.drivers[driver.id] = driver
