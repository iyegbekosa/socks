import factory
from django.contrib.auth import get_user_model

DEFAULT_PASSWORD = "Passw0rd!long"


class StaffFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()
        skip_postgeneration_save = True

    kind = "staff"
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    email = factory.Sequence(lambda n: f"staff{n}@school.ng")
    phone = factory.Sequence(lambda n: f"+23480{n:08d}"[:14])
    password = factory.PostGenerationMethodCall("set_password", DEFAULT_PASSWORD)

    @factory.post_generation
    def save_password(self, create, extracted, **kwargs):
        if create:
            self.save(update_fields=["password"])


class StudentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()
        skip_postgeneration_save = True

    kind = "student"
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    admission_number = factory.Sequence(lambda n: f"GFS/2026/{n:04d}")
    recovery_phone = "+2348031234567"
    password = factory.PostGenerationMethodCall("set_password", DEFAULT_PASSWORD)

    @factory.post_generation
    def save_password(self, create, extracted, **kwargs):
        if create:
            self.save(update_fields=["password"])
