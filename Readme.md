# Socket Wizard

La recepcionista de **Clínica Arenal**.

Alguien llama. Ella contesta, entiende qué necesita y lo deja hecho: una cita, un cambio, una cancelación o un alta. Habla como en una clínica de verdad, no como un formulario.

## Qué hace

- Atiende la llamada en español, inglés o catalán, y sigue el idioma de quien llama.
- Reconoce al paciente y usa su ficha: no pregunta lo que ya consta.
- Busca huecos reales y reserva con el médico y el centro que tocan.
- Respeta las normas de la clínica (seguro, edad, derivación). Si no se puede, lo dice con claridad.
- Si es urgente o alguien pide datos de otro paciente, no improvisa: deriva o se niega.
- Al terminar, manda un correo de resumen a la clínica (cita, cambio, baja o alta).

El modelo lleva la conversación. Lo que acaba en el registro lo decide la clínica, no una ocurrencia.

## Cómo se ve

Abre [localhost:7860](http://localhost:7860/). Ahí se ven las llamadas en directo, lo que se dijo, la cita confirmada y el correo.

Para probarlo sin teléfono: **Talk to the Agent** en esa misma pantalla.

## Arrancar

```bash
python -m pip install -r requirements.txt
python -m src.main
```

Claves en `.env` (copia `.env.example`). No subas ese archivo.

Hecho para el track Prosper de HackSpain. El reto se llama *El Turno*; el producto es Socket Wizard.
