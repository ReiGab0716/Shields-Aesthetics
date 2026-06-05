from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Appointment, Service


class ClinicFlowIntegrityTests(TestCase):
    def setUp(self):
        self.patient = User.objects.create_user(
            username="patient1",
            password="pass12345",
            first_name="Maria",
            last_name="Santos",
            email="maria@example.com",
        )
        self.patient.profile.role = "PATIENT"
        self.patient.profile.phone_number = "09123456789"
        self.patient.profile.save()

        self.secretary = User.objects.create_user(username="secretary1", password="pass12345")
        self.secretary.profile.role = "SECRETARY"
        self.secretary.profile.save()

        self.doctor = User.objects.create_user(username="doctor1", password="pass12345")
        self.doctor.profile.role = "DOCTOR"
        self.doctor.profile.save()

        self.service = Service.objects.create(
            name="Consultation",
            category="DOCTOR",
            description="Doctor consultation",
            price=0,
        )

    def next_open_day(self):
        selected = date.today() + timedelta(days=2)
        while selected.weekday() == 6:
            selected += timedelta(days=1)
        return selected

    def next_sunday(self):
        selected = date.today()
        while selected.weekday() != 6:
            selected += timedelta(days=1)
        return selected

    def create_future_appointment(self):
        return Appointment.objects.create(
            patient=self.patient,
            service=self.service,
            reference_number="TRN-0013",
            date=self.next_open_day(),
            time=time(10, 0),
            status="Pending",
            price_at_booking=self.service.price,
        )

    def test_role_based_access_control_blocks_wrong_dashboards(self):
        self.client.login(username="patient1", password="pass12345")
        response = self.client.get(reverse("doctor_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("home"))

        self.client.login(username="secretary1", password="pass12345")
        for url_name in ("doctor_sales", "doctor_manage_users"):
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, reverse("home"))

    def test_authenticated_pages_and_logout_send_no_store_headers(self):
        self.client.login(username="patient1", password="pass12345")
        dashboard_response = self.client.get(reverse("patient_dashboard"))
        self.assertIn("no-store", dashboard_response["Cache-Control"])

        logout_response = self.client.get(reverse("logout"))
        self.assertEqual(logout_response.status_code, 302)
        self.assertIn("no-store", logout_response["Cache-Control"])

        back_response = self.client.get(reverse("patient_dashboard"))
        self.assertEqual(back_response.status_code, 302)
        self.assertIn(reverse("login"), back_response.url)

    def test_legacy_staff_set_appointment_urls_redirect_to_unified_scheduler(self):
        self.client.login(username="doctor1", password="pass12345")
        response = self.client.get(reverse("doctor_set_appointment"))
        self.assertRedirects(response, reverse("schedule_appointment"))

        self.client.login(username="secretary1", password="pass12345")
        response = self.client.get(reverse("secretary_set_appointment"))
        self.assertRedirects(response, reverse("schedule_appointment"))

    def test_staff_search_finds_full_trn_and_patient_ids(self):
        appointment = self.create_future_appointment()

        self.client.login(username="doctor1", password="pass12345")
        response = self.client.get(reverse("doctor_manage_sessions"), {"search": "TRN-0013"})
        self.assertContains(response, appointment.reference_number)

        patient_id = f"#SA-{self.patient.id:04d}"
        response = self.client.get(reverse("doctor_manage_sessions"), {"search": patient_id})
        self.assertContains(response, patient_id)

        self.client.login(username="secretary1", password="pass12345")
        response = self.client.get(reverse("secretary_manage_sessions"), {"search": "TRN-0013"})
        self.assertContains(response, appointment.reference_number)

    def test_booking_rejects_sundays_and_one_hour_lead_time(self):
        self.client.login(username="secretary1", password="pass12345")
        sunday_response = self.client.post(reverse("schedule_appointment"), {
            "patient_id": self.patient.id,
            "service_id": self.service.id,
            "date": self.next_sunday().isoformat(),
            "time": "10:00:00",
        })
        self.assertEqual(sunday_response.status_code, 400)
        self.assertIn("Sundays are rest days", sunday_response.json()["error"])

        fixed_now = timezone.make_aware(datetime(2026, 5, 11, 9, 0), timezone.get_current_timezone())
        with patch("clinic.views.timezone.localtime", return_value=fixed_now):
            soon = fixed_now + timedelta(minutes=30)
            lead_time_response = self.client.post(reverse("schedule_appointment"), {
                "patient_id": self.patient.id,
                "service_id": self.service.id,
                "date": soon.date().isoformat(),
                "time": soon.strftime("%H:%M:%S"),
            })
        self.assertEqual(lead_time_response.status_code, 400)
        self.assertIn("at least 1 hour", lead_time_response.json()["error"])

    def test_manual_registration_credentials_are_returned_for_schedule_flow(self):
        self.client.login(username="secretary1", password="pass12345")
        register_response = self.client.post(reverse("quick_register_patient"), {
            "name": "Juan Dela Cruz",
            "phone": "09987654321",
            "email": "",
        })
        self.assertEqual(register_response.status_code, 200)
        payload = register_response.json()["patient"]
        self.assertTrue(payload["username"])
        self.assertTrue(payload["temporary_password"])

        appointment_response = self.client.post(reverse("schedule_appointment"), {
            "patient_id": payload["id"],
            "service_id": self.service.id,
            "date": self.next_open_day().isoformat(),
            "time": "14:00:00",
            "patient_username": payload["username"],
            "temporary_password": payload["temporary_password"],
        })
        self.assertEqual(appointment_response.status_code, 200)
        appointment_payload = appointment_response.json()
        self.assertEqual(appointment_payload["patient_username"], payload["username"])
        self.assertEqual(appointment_payload["temporary_password"], payload["temporary_password"])

    def test_patient_flow_from_home_to_booking_and_dashboard(self):
        home_response = self.client.get(reverse("home"))
        self.assertContains(home_response, 'id="contact"')
        self.assertContains(home_response, "Set Appointment")
        self.assertContains(
            home_response,
            'href="#contact" class="btn-appointment">Set Appointment',
            count=2,
        )
        self.assertContains(home_response, "Consultation")
        self.assertContains(home_response, 'data-category="DOCTOR"')

        self.client.login(username="patient1", password="pass12345")
        patient_home_response = self.client.get(reverse("home"))
        self.assertContains(patient_home_response, f'href="{reverse("book_appointment")}"', count=2)

        booking_response = self.client.post(reverse("book_appointment"), {
            "service_id": self.service.id,
            "date": self.next_open_day().isoformat(),
            "time": "09:00:00",
            "skinConcerns": "Routine consultation",
        })
        self.assertEqual(booking_response.status_code, 200)
        self.assertEqual(booking_response.json()["status"], "success")

        dashboard_response = self.client.get(reverse("patient_dashboard"))
        self.assertContains(dashboard_response, "TRN-")

    def test_homepage_set_appointment_links_follow_staff_roles(self):
        self.client.login(username="doctor1", password="pass12345")
        doctor_response = self.client.get(reverse("home"))
        self.assertContains(doctor_response, f'href="{reverse("schedule_appointment")}"', count=2)

        self.client.login(username="secretary1", password="pass12345")
        secretary_response = self.client.get(reverse("home"))
        self.assertContains(secretary_response, f'href="{reverse("schedule_appointment")}"', count=2)

    def test_secretary_flow_reaches_manual_registration_dashboard_and_sessions(self):
        self.client.login(username="secretary1", password="pass12345")

        dashboard_response = self.client.get(reverse("secretary_dashboard"))
        self.assertEqual(dashboard_response.status_code, 200)

        register_response = self.client.post(reverse("quick_register_patient"), {
            "name": "Ana Reyes",
            "phone": "09111222333",
            "email": "ana.reyes@example.com",
        })
        self.assertEqual(register_response.status_code, 200)
        self.assertTrue(register_response.json()["patient"]["temporary_password"])

        sessions_response = self.client.get(reverse("secretary_manage_sessions"))
        self.assertEqual(sessions_response.status_code, 200)
        self.assertContains(sessions_response, "Manage Sessions")

    def test_doctor_flow_reaches_clinical_detail_sales_and_manage_users(self):
        appointment = self.create_future_appointment()
        self.client.login(username="doctor1", password="pass12345")

        dashboard_response = self.client.get(reverse("doctor_dashboard"))
        self.assertEqual(dashboard_response.status_code, 200)

        detail_response = self.client.get(reverse("patient_detail", args=[self.patient.id]))
        self.assertContains(detail_response, appointment.reference_number)

        notes_response = self.client.post(reverse("save_doctor_notes", args=[appointment.id]), {
            "notes": "Clinical notes from simulation."
        })
        self.assertEqual(notes_response.status_code, 200)
        self.assertEqual(notes_response.json()["status"], "success")

        sales_response = self.client.get(reverse("doctor_sales"))
        self.assertEqual(sales_response.status_code, 200)

        users_response = self.client.get(reverse("doctor_manage_users"))
        self.assertContains(users_response, "#SA-")

    def test_profile_update_allows_unchanged_original_email(self):
        duplicate = User.objects.create_user(
            username="duplicate-email",
            password="pass12345",
            first_name="Other",
            last_name="Account",
            email=self.patient.email,
        )
        duplicate.profile.role = "PATIENT"
        duplicate.profile.save()

        self.client.login(username="patient1", password="pass12345")
        response = self.client.post(reverse("patient_profile"), {
            "update_profile": "1",
            "first_name": self.patient.first_name,
            "last_name": self.patient.last_name,
            "username": self.patient.username,
            "email": self.patient.email,
            "phone": self.patient.profile.phone_number,
        })

        self.assertRedirects(response, reverse("patient_profile"))
        self.assertFalse(
            any("Email is already used" in str(message) for message in response.wsgi_request._messages)
        )

    def test_status_updates_redirect_back_to_current_filtered_page(self):
        appointment = self.create_future_appointment()
        self.client.login(username="doctor1", password="pass12345")
        next_url = f'{reverse("doctor_manage_sessions")}?status=pending&search=TRN-0013'

        response = self.client.post(reverse("doctor_confirm_appointment", args=[appointment.id]), {
            "next": next_url,
        })

        self.assertRedirects(response, next_url, fetch_redirect_response=False)

    def test_custom_models_use_readable_postgres_table_names(self):
        self.assertEqual(Service._meta.db_table, "services")
        self.assertEqual(Appointment._meta.db_table, "appointments")
        self.assertEqual(User._meta.get_field("profile").related_model._meta.db_table, "user_profiles")
