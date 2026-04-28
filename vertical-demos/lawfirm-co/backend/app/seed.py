from sqlalchemy.orm import Session
from . import models
from faker import Faker
import random
from .core import get_logger

fake = Faker("en_UK")

logger = get_logger(__name__)


def seed_db(db: Session):
    # Only seed if database is empty (no lawyers exist)
    existing_lawyers = db.query(models.Lawyer).count()
    if existing_lawyers > 0:
        logger.info("Database already has data, skipping seed.")
        return

    logger.info("Seeding database with sample data...")

    # Clear existing data to ensure fresh seed (only runs if we're seeding)
    db.query(models.Document).delete()
    db.query(models.Evidence).delete()
    db.query(models.Case).delete()
    db.query(models.Lawyer).delete()
    db.commit()

    # Create Lawyers
    lawyers = []
    specializations = [
        "Homicide",
        "Fraud",
        "Cybercrime",
        "Narcotics",
        "General Litigation",
    ]
    for _ in range(15):
        full_name = fake.name()
        # Create email: firstname.lastname@justitia.co.uk
        email_name = full_name.lower().replace(" ", ".").replace("..", ".")
        email = f"{email_name}@justitia.co.uk"

        lawyer = models.Lawyer(
            full_name=full_name,
            email=email,
            specialization=random.choice(specializations),
        )
        db.add(lawyer)
        lawyers.append(lawyer)
    db.commit()

    # Refresh to get IDs
    for l in lawyers:
        db.refresh(l)

    # Create Cases
    case_types = ["Fraud", "Homicide", "Theft", "Assault", "Cybercrime", "Narcotics"]
    statuses = ["Open", "Closed", "Pending Trial", "Under Investigation"]

    for _ in range(20):
        lawyer = random.choice(lawyers)
        case_type = random.choice(case_types)
        firstname = fake.first_name()
        lastname = fake.last_name()

        case = models.Case(
            title=f"The King v. {lastname} - {case_type}",
            description=fake.paragraph(nb_sentences=5),
            status=random.choice(statuses),
            case_type=case_type,
            defendant_name=f"{firstname} {lastname}",
            lead_attorney_id=lawyer.id,
            date_opened=fake.date_time_between(start_date="-2y", end_date="now"),
        )
        db.add(case)
        db.commit()
        db.refresh(case)

        # Add Evidence
        for _ in range(random.randint(1, 5)):
            evidence = models.Evidence(
                description=fake.sentence(),
                evidence_type=random.choice(
                    ["Physical", "Digital", "Testimonial", "Forensic"]
                ),
                location_found=fake.address(),
                collected_date=fake.date_time_between(
                    start_date=case.date_opened, end_date="now"
                ),
                case_id=case.id,
            )
            db.add(evidence)

        # Add Documents
        for _ in range(random.randint(1, 3)):
            doc_type = random.choice(
                [
                    "Indictment",
                    "Witness Statement",
                    "Forensic Report",
                    "Court Order",
                    "Evidence Log",
                ]
            )

            # Generate context-aware content
            content = ""
            if case_type == "Fraud":
                content = f"FINANCIAL AUDIT REPORT\n\nSubject: {case.title}\nDate: {fake.date()}\n\nPreliminary analysis of the defendant's bank records reveals a series of irregular transactions totaling over $1.5M. These funds were routed through shell companies in offshore jurisdictions. The following discrepancies were noted in the quarterly filings..."
            elif case_type == "Homicide":
                content = f"AUTOPSY REPORT / WITNESS TESTIMONY\n\nCase: {case.title}\n\nThe victim was found at the scene with multiple injuries consistent with the weapon recovered. Witness A stated that they observed the defendant leaving the premises at approximately 23:00 hours on the night of the incident. Forensic analysis confirms the presence of DNA matching the defendant..."
            elif case_type == "Cybercrime":
                content = f"DIGITAL FORENSICS REPORT\n\nTarget: {case.defendant_name}\n\nAnalysis of the seized server logs indicates unauthorized access originating from IP addresses traced back to the defendant's residence. Encrypted payloads were discovered in the /var/www/html directory, designed to exfiltrate user data. Decryption keys were recovered from the defendant's personal laptop..."
            elif case_type == "Narcotics":
                content = f"SEIZURE REPORT\n\nIncident Date: {fake.date()}\n\nOfficers executed a search warrant at the defendant's property. Recovered items include 5kg of a white powdery substance, later confirmed as cocaine, along with packaging materials and a large sum of cash. The defendant was apprehended while attempting to flee out the back exit..."
            else:
                content = f"OFFICIAL LEGAL DOCUMENT\n\nCase: {case.title}\n\nThis document serves as a formal record of the proceedings regarding the aforementioned case. All parties are hereby notified of the upcoming hearing dates. The evidence presented herein has been cataloged and stored in accordance with chain of custody procedures.\n\n{fake.paragraph(nb_sentences=5)}"

            # Add a second paragraph of filler to make it look substantial
            content += f"\n\nFURTHER DETAILS:\n{fake.paragraph(nb_sentences=8)}"

            doc = models.Document(
                title=f"{doc_type} - {fake.file_name(extension='txt')}",
                content=content,
                created_date=fake.date_time_between(
                    start_date=case.date_opened, end_date="now"
                ),
                case_id=case.id,
            )
            db.add(doc)

    db.commit()
