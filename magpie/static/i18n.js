// Magpie localization. The app's own text is looked up by its English wording (missing = English), so a new string works
// untranslated until someone adds a row below. Each row: English, then Hebrew, Spanish, German, French.
// Movie and TV texts (title, description, genres) come translated from the server: see localizedView().
const LANGUAGES = { en: "English", he: "עברית", es: "Español", de: "Deutsch", fr: "Français" };
const RTL_LANGUAGES = new Set(["he", "ar"]);
const TRANSLATIONS = (() => {
  const order = ["he", "es", "de", "fr"];
  const rows = [
    ["Search everything you saved", "חפש בכל מה ששמרת", "Busca todo lo que guardaste", "Alles Gespeicherte durchsuchen", "Rechercher dans tout ce que vous avez enregistré"],
    ["Movies & TV", "סרטים וסדרות", "Cine y TV", "Filme & Serien", "Films et séries"],
    ["Repos", "מאגרים", "Repos", "Repos", "Dépôts"],
    ["Recipes", "מתכונים", "Recetas", "Rezepte", "Recettes"],
    ["Books", "ספרים", "Libros", "Bücher", "Livres"],
    ["Music & podcasts", "מוזיקה ופודקאסטים", "Música y pódcast", "Musik & Podcasts", "Musique et podcasts"],
    ["Articles & videos", "מאמרים וסרטונים", "Artículos y vídeos", "Artikel & Videos", "Articles et vidéos"],
    ["Places & events", "מקומות ואירועים", "Lugares y eventos", "Orte & Veranstaltungen", "Lieux et événements"],
    ["Release date", "תאריך יציאה", "Fecha de estreno", "Erscheinungsdatum", "Date de sortie"],
    ["Genres", "ז'אנרים", "Géneros", "Genres", "Genres"],
    ["Cast", "שחקנים", "Reparto", "Besetzung", "Distribution"],
    ["Directors", "במאים", "Dirección", "Regie", "Réalisation"],
    ["Creators", "יוצרים", "Creadores", "Erfinder", "Créateurs"],
    ["Runtime", "משך", "Duración", "Laufzeit", "Durée"],
    ["Tagline", "סלוגן", "Eslogan", "Slogan", "Accroche"],
    ["Year", "שנה", "Año", "Jahr", "Année"],
    ["Network or studio", "רשת או אולפן", "Cadena o estudio", "Sender oder Studio", "Chaîne ou studio"],
    ["Where to watch", "איפה לצפות", "Dónde verla", "Wo ansehen", "Où regarder"],
    ["First air date", "תאריך שידור ראשון", "Primera emisión", "Erstausstrahlung", "Première diffusion"],
    ["Seasons", "עונות", "Temporadas", "Staffeln", "Saisons"],
    ["Episodes", "פרקים", "Episodios", "Folgen", "Épisodes"],
    ["Status", "סטטוס", "Estado", "Status", "Statut"],
    ["Author", "מחבר", "Autor", "Autor", "Auteur"],
    ["Pages", "עמודים", "Páginas", "Seiten", "Pages"],
    ["First published", "פורסם לראשונה", "Primera publicación", "Erstveröffentlichung", "Première publication"],
    ["Details", "פרטים", "Detalles", "Details", "Détails"],
    ["Film", "סרט", "Película", "Film", "Film"],
    ["Series", "סדרה", "Serie", "Serie", "Série"],
    ["Repository", "מאגר", "Repositorio", "Repository", "Dépôt"],
    ["Release", "יציאה", "Lanzamiento", "Veröffentlichung", "Sortie"],
    ["Show", "תוכנית", "Programa", "Sendung", "Émission"],
    ["Corrected by you", "תוקן על ידך", "Corregido por ti", "Von dir korrigiert", "Corrigé par vous"],
    ["Why did you save this? Who recommended it?", "למה שמרת את זה? מי המליץ?", "¿Por qué lo guardaste? ¿Quién lo recomendó?", "Warum hast du das gespeichert? Wer hat es empfohlen?", "Pourquoi l'avez-vous enregistré ? Qui l'a recommandé ?"],
    ["The original isn't on this device", "המקור אינו במכשיר זה", "El original no está en este dispositivo", "Das Original ist nicht auf diesem Gerät", "L'original n'est pas sur cet appareil"],
    [". It opens once you're connected to the server.", ". הוא ייפתח כשתתחבר לשרת.", ". Se abrirá cuando te conectes al servidor.", ". Es öffnet sich, sobald du mit dem Server verbunden bist.", ". Il s'ouvrira une fois connecté au serveur."],
    ["Archived", "בארכיון", "Archivado", "Archiviert", "Archivé"],
    ["Updated {when}", "עודכן {when}", "Actualizado {when}", "Aktualisiert {when}", "Mis à jour {when}"],
    ["Forks", "פורקים", "Forks", "Forks", "Forks"],
    ["Open issues", "בעיות פתוחות", "Incidencias abiertas", "Offene Issues", "Tickets ouverts"],
    ["Search", "חיפוש", "Buscar", "Suchen", "Rechercher"],
    ["All", "הכול", "Todo", "Alle", "Tout"],
    ["To check", "לבדיקה", "Por revisar", "Zu prüfen", "À vérifier"],
    ["Check", "בדיקה", "Revisar", "Prüfen", "Vérifier"],
    ["Tags", "תגיות", "Etiquetas", "Tags", "Étiquettes"],
    ["Add", "הוסף", "Añadir", "Hinzufügen", "Ajouter"],
    ["More ways to add", "דרכים נוספות להוספה", "Más formas de añadir", "Weitere Möglichkeiten", "Autres façons d'ajouter"],
    ["Screenshot or link", "צילום מסך או קישור", "Captura o enlace", "Screenshot oder Link", "Capture ou lien"],
    ["Movie", "סרט", "Película", "Film", "Film"],
    ["TV show", "סדרה", "Serie", "Serie", "Série"],
    ["Book", "ספר", "Libro", "Buch", "Livre"],
    ["GitHub repo", "מאגר GitHub", "Repo de GitHub", "GitHub-Repo", "Dépôt GitHub"],
    ["Recipe", "מתכון", "Receta", "Rezept", "Recette"],
    ["Music", "מוזיקה", "Música", "Musik", "Musique"],
    ["Podcast", "פודקאסט", "Pódcast", "Podcast", "Podcast"],
    ["Video", "וידאו", "Vídeo", "Video", "Vidéo"],
    ["Article", "מאמר", "Artículo", "Artikel", "Article"],
    ["Product", "מוצר", "Producto", "Produkt", "Produit"],
    ["Place", "מקום", "Lugar", "Ort", "Lieu"],
    ["Event", "אירוע", "Evento", "Veranstaltung", "Événement"],
    ["App", "אפליקציה", "App", "App", "Appli"],
    ["Course", "קורס", "Curso", "Kurs", "Cours"],
    ["Other", "אחר", "Otro", "Sonstiges", "Autre"],
    ["Add to Magpie", "הוסף ל-Magpie", "Añadir a Magpie", "Zu Magpie hinzufügen", "Ajouter à Magpie"],
    ["Choose screenshots", "בחר צילומי מסך", "Elegir capturas", "Screenshots wählen", "Choisir des captures"],
    ["Or paste (Ctrl/⌘+V) or drop them anywhere on the page.", "או הדבק (Ctrl/⌘+V) או גרור לכל מקום בדף.", "O pega (Ctrl/⌘+V) o suéltalas en cualquier parte de la página.", "Oder einfügen (Strg/⌘+V) oder irgendwo auf der Seite ablegen.", "Ou collez (Ctrl/⌘+V) ou déposez-les n'importe où sur la page."],
    ["Or paste a link: https://…", "או הדבק קישור: https://…", "O pega un enlace: https://…", "Oder Link einfügen: https://…", "Ou collez un lien : https://…"],
    ["Save link", "שמור קישור", "Guardar enlace", "Link speichern", "Enregistrer le lien"],
    ["Note for this one (optional), e.g. Dana recommended it", "הערה (לא חובה), למשל: דנה המליצה", "Nota (opcional), p. ej. Dana lo recomendó", "Notiz (optional), z. B. Dana hat es empfohlen", "Note (facultative), p. ex. Dana l'a recommandé"],
    ["Close", "סגור", "Cerrar", "Schließen", "Fermer"],
    ["Enlarge cover", "הגדל תמונת שער", "Agrandar portada", "Cover vergrößern", "Agrandir la couverture"],
    ["Smaller cover", "הקטן תמונת שער", "Reducir portada", "Cover verkleinern", "Réduire la couverture"],
    ["View full image", "הצג תמונה מלאה", "Ver imagen completa", "Vollbild anzeigen", "Afficher l'image complète"],
    ["Drop screenshots or a link to add them", "גרור צילומי מסך או קישור כדי להוסיף", "Suelta capturas o un enlace para añadirlos", "Screenshots oder Link hier ablegen", "Déposez des captures ou un lien pour les ajouter"],
    ["Add a movie", "הוסף סרט", "Añadir una película", "Film hinzufügen", "Ajouter un film"],
    ["Add a TV show", "הוסף סדרה", "Añadir una serie", "Serie hinzufügen", "Ajouter une série"],
    ["Add a book", "הוסף ספר", "Añadir un libro", "Buch hinzufügen", "Ajouter un livre"],
    ["Movie title", "שם הסרט", "Título de la película", "Filmtitel", "Titre du film"],
    ["TV show title", "שם הסדרה", "Título de la serie", "Serientitel", "Titre de la série"],
    ["Book title (and author)", "שם הספר (והסופר)", "Título del libro (y autor)", "Buchtitel (und Autor)", "Titre du livre (et auteur)"],
    ["Searching…", "מחפש…", "Buscando…", "Suche läuft…", "Recherche…"],
    ["Nothing found.", "לא נמצא דבר.", "No se encontró nada.", "Nichts gefunden.", "Rien trouvé."],
    ["Add “{q}” as typed", "הוסף “{q}” כפי שהוקלד", "Añadir “{q}” tal cual", "„{q}“ wie eingegeben hinzufügen", "Ajouter « {q} » tel quel"],
    ["Look it up after adding", "יחופש לאחר ההוספה", "Se buscará después de añadirlo", "Wird nach dem Hinzufügen gesucht", "Recherché après l'ajout"],
    ["Searching needs a connection.", "חיפוש דורש חיבור.", "Buscar requiere conexión.", "Die Suche braucht eine Verbindung.", "La recherche nécessite une connexion."],
    ["Nothing here yet. Tap + Add, or paste a screenshot or link.", "אין כאן כלום עדיין. הקש ＋ הוסף, או הדבק צילום מסך או קישור.", "Todavía no hay nada. Toca + Añadir, o pega una captura o enlace.", "Noch nichts da. Tippe auf + Hinzufügen oder füge einen Screenshot oder Link ein.", "Rien ici pour l'instant. Touchez + Ajouter, ou collez une capture ou un lien."],
    ["Nothing matches. Clear the search or switch tabs.", "שום דבר לא מתאים. נקה את החיפוש או החלף לשונית.", "Nada coincide. Borra la búsqueda o cambia de pestaña.", "Nichts passt. Suche löschen oder Tab wechseln.", "Aucun résultat. Effacez la recherche ou changez d'onglet."],
    ["Wrong? Fix it", "שגוי? תקן", "¿Mal? Corrígelo", "Falsch? Korrigieren", "Faux ? Corriger"],
    ["Is this wrong? Fix it", "שגוי? תקן", "¿Incorrecto? Corrígelo", "Falsch? Korrigieren", "Incorrect ? Corriger"],
    ["More", "עוד", "Más", "Mehr", "Plus"],
    ["Refresh metadata", "רענן מידע", "Actualizar metadatos", "Metadaten aktualisieren", "Actualiser les métadonnées"],
    ["Re-analyze", "נתח מחדש", "Volver a analizar", "Neu analysieren", "Réanalyser"],
    ["Type", "סוג", "Tipo", "Typ", "Type"],
    ["Delete", "מחק", "Eliminar", "Löschen", "Supprimer"],
    ["About", "אודות", "Acerca de", "Info", "À propos"],
    ["Original", "מקור", "Original", "Original", "Original"],
    ["Your note", "ההערה שלך", "Tu nota", "Deine Notiz", "Votre note"],
    ["How sure Magpie is", "עד כמה Magpie בטוח", "Qué tan seguro está Magpie", "Wie sicher sich Magpie ist", "Degré de certitude de Magpie"],
    ["No description yet.", "אין תיאור עדיין.", "Aún no hay descripción.", "Noch keine Beschreibung.", "Pas encore de description."],
    [" Refresh metadata may find one.", " רענון המידע עשוי למצוא.", " Actualizar metadatos puede encontrar una.", " Metadaten aktualisieren findet vielleicht eine.", " Actualiser les métadonnées peut en trouver une."],
    ["Your screenshot", "צילום המסך שלך", "Tu captura", "Dein Screenshot", "Votre capture"],
    ["View full size", "הצג בגודל מלא", "Ver tamaño completo", "Volle Größe anzeigen", "Voir en taille réelle"],
    ["Open {host}", "פתח {host}", "Abrir {host}", "{host} öffnen", "Ouvrir {host}"],
    ["Digital release", "יציאה דיגיטלית", "Estreno digital", "Digitale Veröffentlichung", "Sortie numérique"],
    ["In cinemas", "בקולנוע", "En cines", "Im Kino", "Au cinéma"],
    ["Physical release", "יציאה פיזית", "Lanzamiento físico", "Physische Veröffentlichung", "Sortie physique"],
    ["Not announced yet", "טרם הוכרז", "Aún sin anunciar", "Noch nicht angekündigt", "Pas encore annoncée"],
    ["Next episode", "הפרק הבא", "Próximo episodio", "Nächste Folge", "Prochain épisode"],
    ["Last aired", "שודר לאחרונה", "Última emisión", "Zuletzt ausgestrahlt", "Dernière diffusion"],
    ["First aired", "שודר לראשונה", "Primera emisión", "Erstausstrahlung", "Première diffusion"],
    ["Show status", "סטטוס הסדרה", "Estado de la serie", "Serienstatus", "Statut de la série"],
    ["Retry", "נסה שוב", "Reintentar", "Erneut versuchen", "Réessayer"],
    ["Dismiss", "סגור", "Descartar", "Schließen", "Ignorer"],
    ["Analyzing {name}…", "מנתח את {name}…", "Analizando {name}…", "{name} wird analysiert…", "Analyse de {name}…"],
    ["Identified {name}", "{name} זוהה", "Identificado {name}", "{name} erkannt", "{name} identifié"],
    ["Couldn't identify {name}", "לא ניתן לזהות את {name}", "No se pudo identificar {name}", "{name} konnte nicht erkannt werden", "Impossible d'identifier {name}"],
    ["Re-analyzing {name}…", "מנתח מחדש את {name}…", "Reanalizando {name}…", "{name} wird neu analysiert…", "Nouvelle analyse de {name}…"],
    ["Re-analyzed {name}", "{name} נותח מחדש", "Reanalizado {name}", "{name} neu analysiert", "{name} réanalysé"],
    ["Couldn't re-analyze {name}", "לא ניתן לנתח מחדש את {name}", "No se pudo reanalizar {name}", "{name} konnte nicht neu analysiert werden", "Impossible de réanalyser {name}"],
    ["Applying your correction to {name}…", "מחיל את התיקון שלך על {name}…", "Aplicando tu corrección a {name}…", "Deine Korrektur für {name} wird angewendet…", "Application de votre correction à {name}…"],
    ["Updated {name}", "{name} עודכן", "Actualizado {name}", "{name} aktualisiert", "{name} mis à jour"],
    ["Couldn't apply the correction to {name}", "לא ניתן להחיל את התיקון על {name}", "No se pudo aplicar la corrección a {name}", "Korrektur für {name} fehlgeschlagen", "Impossible d'appliquer la correction à {name}"],
    ["Refreshing metadata for {name}…", "מרענן מידע על {name}…", "Actualizando metadatos de {name}…", "Metadaten für {name} werden aktualisiert…", "Actualisation des métadonnées de {name}…"],
    ["Refreshed metadata for {name}", "המידע על {name} רוענן", "Metadatos de {name} actualizados", "Metadaten für {name} aktualisiert", "Métadonnées de {name} actualisées"],
    ["Couldn't refresh metadata for {name}", "לא ניתן לרענן מידע על {name}", "No se pudieron actualizar los metadatos de {name}", "Metadaten für {name} konnten nicht aktualisiert werden", "Impossible d'actualiser les métadonnées de {name}"],
    ["Theme", "ערכת נושא", "Tema", "Design", "Thème"],
    ["System", "מערכת", "Sistema", "System", "Système"],
    ["Light", "בהיר", "Claro", "Hell", "Clair"],
    ["Dark", "כהה", "Oscuro", "Dunkel", "Sombre"],
    ["Language", "שפה", "Idioma", "Sprache", "Langue"],
    ["This device only.", "במכשיר זה בלבד.", "Solo este dispositivo.", "Nur dieses Gerät.", "Cet appareil uniquement."],
    ["Automatic", "אוטומטי", "Automático", "Automatisch", "Automatique"],
  ];
  const out = Object.fromEntries(order.map((l) => [l, {}]));
  for (const [en, ...texts] of rows) order.forEach((l, i) => { out[l][en] = texts[i]; });
  return out;
})();

function supportedLanguage(code) {
  const base = String(code || "").toLowerCase().split("-")[0];
  return LANGUAGES[base] ? base : null;
}

// This device's choice (Settings → Appearance), else the browser's language, else English.
function getLanguage() {
  try {
    const saved = localStorage.getItem("magpie.lang");
    if (saved && supportedLanguage(saved)) return saved;
  } catch {}
  for (const code of navigator.languages || [navigator.language]) {
    const found = supportedLanguage(code);
    if (found) return found;
  }
  return "en";
}

let uiLanguage = getLanguage();

function tr(english, vars) {
  let text = (TRANSLATIONS[uiLanguage] || {})[english] ?? english;
  if (vars) text = text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));
  return text;
}

// Fills the page's fixed text: data-i18n="English text" and data-i18n-attr="placeholder:English text;aria-label:…".
function applyI18n(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = tr(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-attr]").forEach((el) => {
    for (const pair of el.dataset.i18nAttr.split(";")) {
      const [attr, ...rest] = pair.split(":");
      if (attr && rest.length) el.setAttribute(attr.trim(), tr(rest.join(":").trim()));
    }
  });
}

function setUiLanguage(code) {
  uiLanguage = supportedLanguage(code) || "en";
  document.documentElement.lang = uiLanguage;
  document.documentElement.dir = RTL_LANGUAGES.has(uiLanguage) ? "rtl" : "ltr";
  applyI18n();
}

// An item as the viewer should read it: the server stores TMDB's translated title, description, tagline and genres
// (metadata.localized) in its content language; they replace the English ones when the viewer's language is that one.
function localizedView(item) {
  const local = item?.metadata?.localized;
  if (!local || local.lang !== uiLanguage) return item;
  const metadata = { ...item.metadata };
  for (const key of ["tagline", "genres"]) if (local[key]) metadata[key] = local[key];
  if (local.summary && item.summary) metadata.description = metadata.description === item.summary ? local.summary : metadata.description;
  return { ...item, title: local.title || item.title, summary: local.summary || item.summary, metadata };
}

// Dates and "in 12 days" in the viewer's language.
function relativeDays(n) {
  const rtf = new Intl.RelativeTimeFormat(uiLanguage, { numeric: "auto" });
  const a = Math.abs(n);
  return a < 60 ? rtf.format(n, "day") : a < 700 ? rtf.format(Math.round(n / 30), "month") : rtf.format(Math.round(n / 365), "year");
}
