from __future__ import annotations

import re
from datetime import date, timedelta

from v2.domain.identity import normalize_email, normalize_text, parse_national_id, spoken_to_digits
from v2.domain.timeref import resolve_when
from v2.models import Intent, RegistrationFields


FIELDS = {
    "en": {"name": "the patient's full name", "date_of_birth": "date of birth (year, month and day)",
           "national_id": "DNI or NIE, including its check letter", "phone": "nine-digit phone number",
           "given_name": "given name", "first_surname": "first surname", "second_surname": "second surname",
           "email": "complete email address", "insurer": "insurance plan", "insurers": "the name of another insurance plan you hold",
           "specialty_id": "specialty or doctor's name", "when": "the requested date or earliest availability",
           "appointment_id": "which existing appointment (doctor and date)", "doctor_name": "the doctor's full name",
           "identity": "another identifier: date of birth, phone number or DNI/NIE"},
    "es": {"name": "el nombre completo del paciente", "date_of_birth": "fecha de nacimiento (año, mes y día)",
           "national_id": "DNI o NIE, incluida su letra", "phone": "teléfono de nueve dígitos",
           "given_name": "nombre", "first_surname": "primer apellido", "second_surname": "segundo apellido",
           "email": "correo electrónico completo", "insurer": "aseguradora", "insurers": "el nombre de otro seguro que tenga",
           "specialty_id": "especialidad o nombre del médico", "when": "fecha deseada o si quiere la primera disponible",
           "appointment_id": "qué cita existente (médico y fecha)", "doctor_name": "el nombre completo del médico",
           "identity": "otro identificador: fecha de nacimiento, teléfono o DNI/NIE"},
    "ca": {"name": "el nom complet del pacient", "date_of_birth": "data de naixement (any, mes i dia)",
           "national_id": "DNI o NIE, inclosa la lletra", "phone": "telèfon de nou dígits",
           "given_name": "nom", "first_surname": "primer cognom", "second_surname": "segon cognom",
           "email": "correu electrònic complet", "insurer": "asseguradora", "insurers": "el nom d'una altra assegurança que tingui",
           "specialty_id": "especialitat o nom del metge", "when": "data desitjada o si vol la primera disponible",
           "appointment_id": "quina cita existent (metge i data)", "doctor_name": "el nom complet del metge",
           "identity": "un altre identificador: data de naixement, telèfon o DNI/NIE"},
}

REASONS = {
    "not_eligible_age": ("This specialty cannot see a patient of this age. We can look for an age-appropriate service.",
                         "Esta especialidad no atiende a pacientes de esta edad. Podemos buscar el servicio adecuado.",
                         "Aquesta especialitat no atén pacients d'aquesta edat. Podem buscar el servei adequat."),
    "referral_required": ("A referral recorded by the clinic is required for this specialty. Please contact your referring doctor.",
                          "Esta especialidad requiere una derivación registrada en la clínica. Contacte con su médico para obtenerla.",
                          "Aquesta especialitat requereix una derivació registrada a la clínica. Contacti amb el seu metge per obtenir-la."),
    "provider_not_in_network": ("This doctor does not accept your plan. Do you have another plan, or would you consider another doctor?",
                                "Este médico no acepta su seguro. ¿Tiene otro seguro o prefiere otro médico?",
                                "Aquest metge no accepta la seva assegurança. En té una altra o prefereix un altre metge?"),
    "specialty_not_covered": ("Your plan does not cover this specialty. Do you hold another insurance plan?",
                              "Su seguro no cubre esta especialidad. ¿Tiene otro seguro?",
                              "La seva assegurança no cobreix aquesta especialitat. En té una altra?"),
    "location_not_covered": ("Your plan does not cover this site. Do you hold another plan or want a different site?",
                             "Su seguro no cubre esta sede. ¿Tiene otro seguro o prefiere otra sede?",
                             "La seva assegurança no cobreix aquest centre. En té una altra o prefereix un altre centre?"),
    "insurer_referral_required": ("Your insurer requires a referral or authorization before this appointment. Please arrange it with your insurer.",
                                  "Su aseguradora exige derivación o autorización para esta cita. Solicítela a su aseguradora.",
                                  "La seva asseguradora exigeix derivació o autorització per a aquesta cita. Demani-la a l'asseguradora."),
    "allowance_exhausted": ("The covered visit allowance is exhausted. Please contact your insurer or tell me another plan you hold.",
                            "Ha agotado las sesiones cubiertas. Contacte con su aseguradora o indique otro seguro que tenga.",
                            "Ha esgotat les sessions cobertes. Contacti amb l'asseguradora o indiqui una altra assegurança que tingui."),
    "provider_on_leave": ("That doctor is on leave for the requested date. Would another date or doctor work?",
                           "Ese médico está de baja o vacaciones en la fecha solicitada. ¿Prefiere otra fecha u otro médico?",
                           "Aquest metge és de baixa o vacances en la data sol·licitada. Prefereix una altra data o un altre metge?"),
    "location_hours": ("The site is not open at that time. Would another time or site work?",
                       "La sede no abre a esa hora. ¿Prefiere otra hora u otra sede?",
                       "El centre no obre a aquesta hora. Prefereix una altra hora o un altre centre?"),
    "type_not_offered": ("The requested appointment type is not offered. We can check another service.",
                         "No se ofrece ese tipo de cita. Podemos consultar otro servicio.",
                         "No s'ofereix aquest tipus de cita. Podem consultar un altre servei."),
    "patient_history": ("The clinic's recorded visit history does not permit this appointment type. Please ask the clinic to review the record.",
                        "El historial registrado no permite este tipo de cita. Pida a la clínica que revise su historial.",
                        "L'historial registrat no permet aquest tipus de cita. Demani a la clínica que revisi l'historial."),
    "no_availability": ("There is no availability matching those constraints. Would you like another date, doctor or site?",
                         "No hay disponibilidad con esas condiciones. ¿Prefiere otra fecha, médico o sede?",
                         "No hi ha disponibilitat amb aquestes condicions. Prefereix una altra data, metge o centre?"),
    "clinic_closed": ("The clinic is closed on that date. Would you like the next open day?",
                       "La clínica está cerrada ese día. ¿Prefiere el siguiente día de apertura?",
                       "La clínica està tancada aquell dia. Prefereix el proper dia d'obertura?"),
    "patient_not_found": ("I could not find a matching patient. Please check the details, give another identifier, or request registration.",
                           "No encuentro un paciente con esos datos. Revise los datos, facilite otro identificador o solicite el alta.",
                           "No trobo cap pacient amb aquestes dades. Revisi-les, doni un altre identificador o demani l'alta."),
    "provider_not_found": ("I could not find that doctor. Please give the full name or request a specialty.",
                            "No encuentro a ese médico. Dígame el nombre completo o la especialidad.",
                            "No trobo aquest metge. Digui'm el nom complet o l'especialitat."),
    "caller_not_authorised": ("I cannot make this change without the required authorization. Please contact the clinic.",
                              "No puedo realizar este cambio sin la autorización necesaria. Contacte con la clínica.",
                              "No puc fer aquest canvi sense l'autorització necessària. Contacti amb la clínica."),
    "out_of_scope": ("This request is outside the clinic's appointment services. No appointment change has been made.",
                      "Esta solicitud no corresponde al servicio de citas de la clínica. No se ha modificado ninguna cita.",
                      "Aquesta petició no correspon al servei de cites de la clínica. No s'ha modificat cap cita."),
    "medical_emergency": ("Please seek urgent medical care now; call 112 if needed. Do not wait for a routine appointment.",
                           "Busque atención médica urgente ahora; llame al 112 si lo necesita. No espere a una cita ordinaria.",
                           "Busqui atenció mèdica urgent ara; truqui al 112 si cal. No esperi una cita ordinària."),
}


def reason_text(reason: str | None, language: str) -> str:
    if reason not in REASONS:
        return {"en": "I could not verify the clinic's response. No action has been confirmed; please try again or contact the clinic.",
                "es": "No he podido verificar la respuesta de la clínica. No se ha confirmado ninguna acción; inténtelo de nuevo o contacte con la clínica.",
                "ca": "No he pogut verificar la resposta de la clínica. No s'ha confirmat cap acció; torni-ho a provar o contacti amb la clínica."}[language]
    return REASONS[reason][("en", "es", "ca").index(language)]


def collection_prompt(intent: Intent, language: str) -> str:
    keys = list(dict.fromkeys([*intent.validation_errors, *intent.missing_fields]))
    labels = [FIELDS[language].get(key, FIELDS[language]["identity"]) for key in keys[:3]]
    if not labels:
        labels = [FIELDS[language]["appointment_id" if intent.action in {"cancel", "reschedule"} else "specialty_id"]]
    prefix = {"en": "Please provide ", "es": "Dígame ", "ca": "Digui'm "}[language]
    if intent.validation_errors:
        prefix = {"en": "Please correct or repeat ", "es": "Corrija o repita ", "ca": "Corregeixi o repeteixi "}[language]
    line = prefix + ", ".join(labels) + "."
    if intent.validation_errors.get("when") == "unsupported_window":
        line = {"en": "I cannot safely interpret that range. Please give a single date and an exact time or morning/afternoon.",
                "es": "No puedo interpretar ese intervalo con seguridad. Indique una sola fecha y una hora concreta o mañana/tarde.",
                "ca": "No puc interpretar aquest interval amb seguretat. Indiqui una sola data i una hora concreta o matí/tarda."}[language]
    if intent.validation_errors.get("appointment_id") == "not_found":
        line = {"en": "No matching upcoming appointment was found. ",
                "es": "No se ha encontrado una cita pendiente que coincida. ",
                "ca": "No s'ha trobat cap cita pendent que coincideixi. "}[language] + line
    subject = intent.identity_inputs.get("name") or intent.subject
    line = {"en": f"For {subject}: ", "es": f"Para {subject}: ", "ca": f"Per a {subject}: "}[language] + line
    if intent.choices:
        line += " " + "; ".join(", ".join(str(row[key]) for key in ("doctor", "when", "site") if row.get(key))
                                 for row in intent.choices[:5]) + "."
    return line


def valid_national_id(value: str) -> bool:
    parsed = parse_national_id(value)
    return bool(parsed["valid"] and parsed["cleaned"] == parsed["value"])


def valid_birth(value: str, today: date) -> bool:
    try:
        born = date.fromisoformat(value)
        return born.isoformat() == value and born <= today
    except ValueError:
        return False


def valid_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value) or spoken_to_digits(value)
    if digits.startswith("0034"):
        digits = digits[4:]
    elif digits.startswith("34") and len(digits) == 11:
        digits = digits[2:]
    return len(digits) == 9


def identity_problems(fields: dict, today: date) -> tuple[list[str], dict[str, str]]:
    errors = {}
    for key, value in fields.items():
        valid = bool(str(value).strip())
        if key == "name":
            valid = len(normalize_text(value).split()) >= 2
        elif key == "date_of_birth":
            valid = valid_birth(value, today)
        elif key == "national_id":
            valid = valid_national_id(value)
        elif key == "phone":
            valid = valid_phone(value)
        if not valid:
            errors[key] = "invalid"
    missing = []
    if not fields.get("name") and len(fields) < 2:
        missing.append("name")
    if len(fields) < 2:
        missing.append("date_of_birth" if not fields.get("date_of_birth") else "identity")
    return missing, errors


def registration_plan(engine, fields: RegistrationFields, today: date) -> tuple[dict | None, list[str], dict[str, str]]:
    values = fields.model_dump(exclude_none=True)
    missing = [key for key in RegistrationFields.model_fields if not values.get(key, "").strip()]
    errors = {}
    for key in ("national_id", "date_of_birth", "phone"):
        if key in values:
            _, bad = identity_problems({key: values[key]}, today)
            errors.update(bad)
    if values.get("email") and not re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", normalize_email(values["email"])):
        errors["email"] = "invalid"
    if values.get("insurer") and not engine.resolve_insurer(values["insurer"]):
        errors["insurer"] = "unknown_plan"
    if missing or errors:
        return None, missing, errors
    payload, problems = engine.plan_registration(values)
    if problems:
        return None, [], {"national_id": "invalid"}
    return payload, [], {}


def resolve_request_when(phrase: str | None, now):
    clean = normalize_text(phrase or "")
    for source, target in ((r"\bdema passat\b", "day after tomorrow"), (r"\bdema\b", "tomorrow"),
                           (r"\bavui\b|\bhoy\b|\btoday\b", now.date().isoformat()),
                           (r"\btarda\b", "afternoon"), (r"\bal mes aviat possible\b", "soonest"),
                           (r"\bla setmana que ve\b|\bla setmana vinent\b", (now.date() + timedelta(days=7)).isoformat())):
        clean = re.sub(source, target, clean)
    spec = resolve_when(clean, now)
    explicit = re.search(r"\b\d{4}-\d{2}-\d{2}\b", clean)
    if explicit:
        try:
            spec.target_date = date.fromisoformat(explicit[0])
            spec.matched = "iso_date"
            spec.soonest = False
        except ValueError:
            spec.target_date = None
            spec.matched = "unparsed"
            spec.part_of_day = None
    return spec


def unsupported_time_request(phrase: str | None) -> bool:
    clean = normalize_text(phrase or "")
    for supported in ("day after tomorrow", "lo antes posible", "cuanto antes", "al mes aviat possible"):
        clean = clean.replace(supported, "")
    if len(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", clean)) > 1:
        return True
    return bool(re.search(r"\b(between|until|before|after|entre|hasta|antes|despues|fins|abans|despres)\b", clean))


def requested_clock(phrase: str | None) -> str | None:
    clean = normalize_text(phrase or "")
    match = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", clean)
    if match:
        return f"{int(match[1]):02d}:{match[2]}"
    match = re.search(r"\b(1[0-2]|[1-9])\s*(am|pm)\b", clean)
    if match:
        hour = int(match[1]) % 12 + (12 if match[2] == "pm" else 0)
        return f"{hour:02d}:00"
    return None


def explicit_decline(text: str) -> bool:
    if "?" in text or "¿" in text:
        return False
    clean = re.sub(r"[,.!;:]", " ", normalize_text(text))
    clean = re.sub(r"\s+", " ", clean).strip()
    return bool(re.fullmatch(
        r"(?:no(?: thanks| thank you| gracias| gracies)?|never mind|no other (?:date|doctor|site|plan|insurance)|"
        r"i (?:do not|don't) have (?:another|any other) (?:plan|insurance)|"
        r"no (?:tengo|quiero) (?:otro|otra) (?:seguro|fecha|medico|sede)|"
        r"no (?:tinc|vull) (?:cap )?(?:altra|altre) (?:asseguranca|data|metge|centre)|"
        r"dejelo|deixi-ho|deixeu-ho|that's all|that is all|that is everything|eso es todo|aixo es tot|"
        r"(?:okay |ok |de acuerdo |d'acord )?(?:thank you|thanks|gracias|gracies))", clean))


def confirmation_selection(text: str) -> tuple[bool, int | None, bool]:
    if "?" in text or "¿" in text:
        return False, None, False
    clean = normalize_text(text).replace("’", "'")
    clean = re.sub(r"[,!.;:]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    affirmative = r"(?:yes|si|okay|ok|correct|correcte|correcto|confirmo|confirm|i confirm|d'acord|perfecte|adelante|endavant)"
    numbers = {"one": 1, "first": 1, "uno": 1, "una": 1, "primera": 1, "primer": 1,
               "two": 2, "second": 2, "dos": 2, "dues": 2, "segunda": 2, "segona": 2,
               "three": 3, "third": 3, "tres": 3, "tercera": 3}
    number = r"(?:[1-9]|10|" + "|".join(numbers) + r")"
    option = rf"(?:(?:the |la |el )?(?:option|opcion|opcio) {number}|(?:the |la |el )?{number}(?: (?:option|opcion|opcio))?)"
    collective = r"(?:both|all|ambas|ambos|los dos|las dos|ambdues|totes|tots|les dues)"
    accepted = bool(re.fullmatch(rf"{affirmative}(?: {collective})?(?: {option})?(?: (?:please|por favor|si us plau|thanks|gracias|gracies))?", clean))
    selection = None
    if accepted:
        remainder = re.sub(rf"\b{collective}\b", "", clean)
        for token in remainder.split():
            if token.isdigit() or token in numbers:
                selection = int(token) if token.isdigit() else numbers[token]
    return accepted, selection, bool(re.search(rf"\b{collective}\b", clean))
