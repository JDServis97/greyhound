# Absolut Greyhound · Streamlit

La edición Streamlit es la versión web de Greyhound. La interfaz mantiene la operación disponible aunque una cuenta de correo falle o la base configurada no responda; los datos persistentes en la nube requieren `DATABASE_URL`.

## Ejecutar en tu equipo

```powershell
py -m pip install -r requirements.txt
py -m streamlit run streamlit_app.py
```

## Publicar en Streamlit Community Cloud

1. Sube esta carpeta a un repositorio privado de GitHub.
2. En Streamlit Cloud, selecciona `streamlit_app.py` como archivo principal y Python 3.12.
3. Copia `.streamlit/secrets.toml.example` en **App settings > Secrets** y completa sus valores.
4. Crea una base PostgreSQL externa y define `DATABASE_URL` en Secrets.
5. Publica `tracking_service.py` en un servicio Python separado y configura su URL HTTPS en `TRACKING_BASE_URL`.
6. Registra el dominio público en Google Cloud y usa contraseñas de aplicación de Gmail.

Streamlit Cloud no conserva SQLite ni ejecuta listeners constantes de forma fiable. Por eso la nube usa PostgreSQL y el botón **Sincronizar Gmail ahora**; si necesitas sincronización automática, despliega un worker separado para ejecutar la misma tarea IMAP cada cinco minutos.

Si PostgreSQL no está disponible, Greyhound muestra un aviso y cambia a SQLite temporal para que la interfaz no se caiga; esa base se pierde al reiniciar la instancia, por lo que no debe usarse como almacenamiento de producción.

El QR de Bitsaje no se publica: el teléfono necesita enlazarse con una instancia local dentro de su red privada.
