# Bridge Satel Integra ↔ Tuya ↔ eBUS

Szkielet działającej integracji między centralą Satel Integra 128 (moduł
ETHM-1 Plus), urządzeniami Tuya (lokalnie) oraz danymi z kotła Wolf (eBUS).

## Status tego kodu

To jest **działający szkielet**, nie gotowe "pod klucz" rozwiązanie -
kilka miejsc wymaga uzupełnienia danymi specyficznymi dla Twojej instalacji
(adresy IP, klucze, numery wejść) oraz jednej rzeczy do zweryfikowania:

- **Tryb "http" wirtualnych wejść IP** (`satel.virtual_inputs.mode: http`,
  patrz "Wyjście reguły ruchu: aktywacja wirtualnego wejścia IP Satela"
  niżej) - opcjonalna alternatywa dla domyślnego trybu "library". Jeśli z
  niej korzystasz: dokładny format URL (`satel.virtual_inputs.url_template`
  w `config.example.yaml`) skopiuj z zakładki "Wejścia IP" w DLOADX po
  skonfigurowaniu konkretnego wejścia - firmware może różnić się formatem
  zapytania między wersjami.
- **Wersja biblioteki `satel_integra`** (`bridge/satel_client.py`) - kod jest
  napisany pod NOWE, publiczne API pakietu (linia 1.x, obecnie przypięta
  `>=1.4.0,<2.0.0` w `requirements.txt`): konstruktor `AsyncSatel(host,
  port, integration_key=...)` (bez `loop`, gdzie `integration_key` to
  opcjonalny KLUCZ SZYFROWANIA transmisji - patrz `satel.encryption_key`
  niżej, coś innego niż kod użytkownika w `satel.integration_key`),
  `register_callbacks()` + `await start(enable_monitoring=True)` zamiast
  blokującego `monitor_status()`, wbudowany keepalive/reconnect i
  wykrywanie "centrala zajęta" (`SatelPanelBusyError`). Wymaga Python
  **3.12+** (patrz `Dockerfile` - obraz bazowy to `python:3.12-slim`).
  **To jest ta sama linia API, której obecnie używa integracja Satel w
  Home Assistant** (patrz `homeassistant/components/satel_integra/client.py`)
  - jej źródła są dobrym odniesieniem architektonicznym (patrz niżej).
  Szczegóły migracji i różnice względem starego (0.3.x) API opisuje
  obszerny docstring na górze `bridge/satel_client.py`.
- **Format wiadomości Pulsar dla kamer Tuya** (`bridge/tuya_cloud_events.py`) -
  różni się między modelami kamer, zweryfikuj w zakładce "Cloud Debugging"
  na developer.tuya.com dla swoich urządzeń.

## Testy automatyczne

```bash
pip install -r requirements-dev.txt
pytest
# z raportem pokrycia:
pytest --cov=bridge --cov=dashboard --cov-report=term-missing
```

**946 testów, wszystkie przechodzą, pokrycie kodu ~84%.** Uczciwie o
brakującym pokryciu: moduły będące cienkimi wrapperami na biblioteki
zewnętrzne (`satel_client.py` - satel_integra, `ebus_client.py` -
callbacki paho-mqtt, `tuya_cloud_events.py`/`unifi_protect_events.py` -
odpowiednio tuya-connector-python i uiprotect, `boiler_output.py` -
TuyaOpenAPI) mają niższe pokrycie, bo sensowne przetestowanie ich
wymagałoby rozbudowanego mockowania wnętrza tych bibliotek - realną
weryfikację tych fragmentów daje dopiero test z prawdziwym sprzętem
(patrz sekcja "Status tego kodu" wyżej). Za to logika, która faktycznie
podejmuje decyzje - routing danych, okna czasowe, eskalacja, cooldown,
alarmy progowe, autoryzacja, reconnect/backoff - ma pokrycie 95-100%.

Testy są w pełni izolowane (własna baza SQLite w katalogu tymczasowym per
test, żadnych prawdziwych połączeń sieciowych) - fałszywe (fake) wersje
`TuyaLocalController`/`SatelIntegraClient`/`SceneRunner` w
`tests/conftest.py` pozwalają testować logikę silnika reguł bez
prawdziwych urządzeń.

CI (`.github/workflows/tests.yml`) uruchamia cały zestaw na Pythonie
3.12/3.13 przy każdym push/PR - dolna granica to teraz 3.12, twardy wymóg
`satel-integra>=1.4.0` (patrz "Status tego kodu" wyżej).

## Instalacja

**Docker to JEDYNY oficjalnie wspierany sposób uruchomienia** (działa tak
samo na RPi jak i na zwykłym Linuksie/amd64 - jeden obraz budowany
**lokalnie**, z `docker-compose.yml` w tym repo, nic nigdzie nie jest
publikowane):

```bash
mkdir -p data
sudo chown 1000:1000 data
docker compose up -d --build
```

Otwórz `http://<adres-hosta>:8090/setup` w przeglądarce i przejdź kreator.
Pełny opis (rebuild po `git pull`, logi, profile, sieć LAN, bezpieczeństwo
obrazu) - patrz sekcja "Uruchomienie w Dockerze" niżej. Docker Compose sam
dba o autostart po restarcie hosta (`restart: unless-stopped`) - nie trzeba
osobno konfigurować usługi systemd.

### Instalacja bezpośrednio na hoście (bez Dockera) - PRZESTARZAŁE

Ta ścieżka (venv + ręczna usługa systemd, patrz "Uruchomienie jako usługa
systemd" niżej) **nie jest już oficjalnie wspierana** - projekt zakłada
Docker jako jedyne testowane środowisko uruchomieniowe. Zostaje udokumentowana
niżej wyłącznie dla zaawansowanych użytkowników, którzy świadomie wolą
zarządzać zależnościami Pythona ręcznie (np. brak Dockera na hoście z innych
powodów) - bez gwarancji, że wszystkie funkcje (w tym automatyczny restart
przez panel, patrz "🔄 Zrestartuj bridge i panel teraz") będą działać tak
samo jak pod Dockerem:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Konfiguracja

Dwie drogi - wybierz wygodniejszą:

### A) Kreator w przeglądarce (zalecane na start)

Jeśli konfiguracja jeszcze nie istnieje, panel **automatycznie
przekierowuje** na `/setup` przy pierwszym wejściu - kreator **podzielony
na etapy** (przyciski Wstecz/Dalej, jeden logicznie spójny temat na
stronę), z przyciskami otwierającymi w nowej karcie strony, których
potrzebujesz (developer.tuya.com, ntfy.sh). Wystarczy:

```bash
python -m dashboard.app --config config.db
```

(patrz sekcja "Konfiguracja w bazie danych" niżej - `config.db` to
plik SQLite, nie YAML do ręcznej edycji, ale ma swoją furtkę
eksportu/importu).

i otworzyć `http://<adres-rpi>:8090/` w przeglądarce - trafisz prosto do
kreatora. Po zapisaniu ("Zapisz i uruchom") strona **sama przenosi Cię do
panelu** (`/`) po krótkiej chwili, **już zalogowany** - kreator loguje Cię
od razu tym samym hasłem, ciasteczkiem sesji ustawionym przez serwer
(patrz sekcja "Bezpieczeństwo panelu" niżej), bez ponownego wpisywania.
`/setup` przestaje być dostępne od tego momentu (przekierowuje na `/`).
Panel jest **od razu gotowy** (bez restartu dashboardu) - zostaje tylko
uruchomić główny proces bridge'a (dokładna komenda pojawi się na ekranie
po zapisie, razem z informacją, jak skonfigurować automatyczne
uruchamianie po restarcie RPi - systemd albo Docker).

**Przycisk "🐛 Pokaż debug"** (górny pasek kreatora) pokazuje na bieżąco
dziennik komunikacji sieciowej (żądanie/odpowiedź) dla akcji w bieżącym
kroku - test połączenia, wykrywanie stref/czujników, import z Tuya Cloud,
skan sieci, zapis. Przydatne przy diagnozowaniu problemów bez zaglądania
do logów serwera - hasła/klucze/sekrety są w nim zawsze ukryte.

**Krok 1 (Satel)** ma dwa przyciski:
- **"🔌 Testuj połączenie"** - sprawdza, czy pod podanym adresem/portem w
  ogóle coś odpowiada (TCP), zanim przejdziesz dalej. Uczciwie: to test
  osiągalności, NIE samego kodu użytkownika - protokół integracji Satela
  wysyła go dopiero przy konkretnym poleceniu (uzbrojenie/wyjście), a
  test świadomie niczego takiego nie wykonuje, żeby nic nie zmienić w
  systemie alarmowym. Błędny kod użytkownika ujawni się dopiero przy
  pierwszym uzbrojeniu/rozbrojeniu.
- **"🔍 Wykryj aktywne strefy/wyjścia"** - protokół integracji Satela NIE
  udostępnia pełnej listy skonfigurowanych stref/wyjść wraz z nazwami
  (ograniczenie samego protokołu, nie da się tego obejść) - ten przycisk
  nasłuchuje przez 30 sekund i notuje numery, które w tym czasie
  ZMIENIĄ STAN. Podczas nasłuchu przejdź fizycznie przez pilnowane
  pomieszczenia (otwórz/zamknij drzwi i okna, wywołaj czujki ruchu) -
  wykryte numery trafią jako nowe wiersze niżej (dodaj im nazwy). Strefy,
  które nie zmienią stanu w tym oknie, trzeba dodać ręcznie (numer z
  DLOADX) - to świadome ograniczenie, nie błąd.

**Postęp zapisuje się automatycznie po każdym kliknięciu "Dalej"** (w
pliku roboczym `.setup_draft.json` obok `config.db`) - kreator da się
bezpiecznie przerwać w dowolnym momencie i wrócić do niego później,
nawet z innej karty/przeglądarki w tej samej sieci. Plik roboczy znika
automatycznie po ostatecznym zapisie (albo ręcznie, przyciskiem "Zacznij
od nowa").

Kreator pokrywa zarówno podstawy (Satel, Tuya lokalnie, eBUS/MQTT, hasło
panelu, powiadomienia, opcjonalnie Home Assistant/UniFi Protect/Tuya
Cloud), jak i bardziej zaawansowane rzeczy zbudowane na scenach
(`bridge/scenes.py`, patrz sekcja "Automatyzacje" niżej):

- **Sceny** - nazwane grupy akcji na wielu urządzeniach naraz.
- **Harmonogramy** (`time_window`) - reguła ruchu aktywna tylko w
  wybranych godzinach albo względem wschodu/zachodu słońca.
- **Eskalacja** - kilka naruszeń wejścia Satela w krótkim czasie wywołuje
  mocniejszą scenę zamiast zwykłej akcji.

Te trzy rzeczy trafiają do osobnego dokumentu `automations` w bazie
(patrz sekcja "Konfiguracja w bazie danych" niżej), nie do dokumentu
`config` - i **zostają w pełni edytowalne PO zapisie**, z powrotem w
przeglądarce, w zabezpieczonej hasłem zakładce **🎬 Automatyzacje**
(`/automations`) - nie trzeba wracać do kreatora ani ręcznie do YAML-a,
żeby dodać kolejną scenę czy regułę później. Symulacja obecności (patrz
niżej) jest tam edytowalna od razu, mimo że kreator jej jeszcze nie zbiera.
Szczegóły - sekcja "Automatyzacje" niżej.

**Krok 2 (urządzenia Tuya)** ma trzy sposoby dodawania - ręcznie, skan
sieci LAN (`🔍 Skanuj sieć` - podpowiada ID+IP, bez `local_key`) i
**import z Tuya Cloud** (`☁ Import z Tuya Cloud` - podaj region + Access
ID + Access Secret, pobiera WSZYSTKIE podpięte urządzenia razem z nazwą i
`local_key` jednym kliknięciem; dokładnie to, co robi `tinytuya wizard`,
tylko bez konsoli). **Polecane przy wielu urządzeniach** - ręczne
wpisywanie `local_key` jednego po drugim szybko się nie opłaca. Gdzie
znaleźć Access ID/Secret i jak założyć projekt w Tuya IoT Platform, jeśli
go nie masz - patrz krok 7 (ta sama sekcja, co dla integracji kamer Tuya
Cloud) i sekcja "Konto Tuya Cloud" niżej.

**Krok 3 (eBUS)** ma przełącznik **"Uruchom lokalny broker MQTT dla
mnie"** - po zaznaczeniu adres/port wypełniają się same
(`127.0.0.1:1883`) i przestają być polem do ręcznego ustawienia; kreator
NIE uruchamia sam żadnej usługi systemowej (wymagałoby to uprawnień roota
z poziomu przeglądarki), tylko pokazuje gotowe komendy do wklejenia
(`apt install mosquitto` + `systemctl enable --now`, albo
`docker compose --profile broker up -d mosquitto` dla wdrożeń w Dockerze)
- patrz sekcja "Lokalny broker MQTT" niżej.

Rzeczy, których panel WCIĄŻ nie obsługuje (reguły kamer, reguły
"światło Tuya → Satel", automatyka kotła, alarmy progowe) dostają puste
wartości domyślne w dokumencie `config` - dopisujesz je później przez
eksport/import YAML (patrz "Konfiguracja w bazie danych" niżej) - kreator
nie uruchomi się drugi raz, dopóki konfiguracja istnieje, więc żadne
Twoje ręczne zmiany nie zostaną nadpisane.

### B) Ręczna edycja plików (przed pierwszym uruchomieniem)

Konfiguracja finalnie żyje w bazie `config.db`, ale PRZED pierwszym
uruchomieniem można ją przygotować tak samo jak dawniej - ręcznie
edytowanymi plikami YAML. Przy pierwszym starcie (jeśli `config.db`
jeszcze nie istnieje, a `config.yaml` tak) bridge/panel **migrują je
automatycznie do bazy jednorazowo** i zmieniają nazwy starych plików na
`*.migrated` (żeby było jasne, że przestały być czytane - patrz sekcja
"Konfiguracja w bazie danych"). PO pierwszym uruchomieniu ręczna edycja
tych plików już nic nie robi - dalsze zmiany rób z panelu albo przez
import archiwum .zip (`/api/config/import-zip`, patrz niżej).

1. `cp config.example.yaml config.yaml`
2. `cp sensors.example.yaml sensors.yaml` (lista czujników eBUS - można
   też edytować później z panelu webowego, patrz sekcja "eBUS / ebusd"
   niżej)
3. Uzupełnij sekcje `satel`, `tuya`, reguły itd. w `config.yaml`
   (komentarze w pliku tłumaczą każde pole). Sceny/reguły ruchu/symulację
   obecności, strefy/wyjścia/wejścia IP/partycje Satela i urządzenia Tuya
   możesz zostawić wpisane wprost w `config.yaml` tak jak w przykładzie -
   działa bez zmian - albo od razu przenieść do osobnych plików
   (`automations.yaml`/`satel_fields.yaml`/`tuya_devices.yaml`, patrz
   sekcje "Automatyzacje" i "Panel webowy" niżej), jeśli wolisz mieć te
   rzeczy oddzielone już przed pierwszą migracją.
4. Local key urządzeń Tuya wyciągnij narzędziem:
   ```bash
   python -m tinytuya wizard
   ```
5. Uruchom `python main.py --config config.db` (albo panel) - migracja
   nastąpi automatycznie przy tym pierwszym starcie.

## Konfiguracja w bazie danych

Cała konfiguracja (połączenia, strefy/wyjścia Satela, urządzenia Tuya -
razem z `local_key` - sceny/reguły/harmonogramy, hasło administratora)
żyje w jednym pliku SQLite: **`config.db`** (w tym samym katalogu, co
wcześniej `config.yaml`, wskazywanym przez `--config`). To NIE jest plik
do ręcznej edycji tekstowej - patrz "Kopia zapasowa (eksport/import .zip)"
niżej po sposób na wgląd/edycję/backup poza panelem.

**Dlaczego baza, a nie pliki `.yaml`:** `local_key` urządzeń Tuya (i inne
sekrety - hasła MQTT, Tuya Cloud Access Secret, kod integracji Satela) to
dowolne ciągi znaków. Zapis/odczyt przez `yaml.safe_dump`/`safe_load`
(tak jak robił to panel) jest w pełni bezpieczny dla dowolnych znaków -
ale przy RĘCZNEJ edycji pliku (bez cudzysłowów wokół wartości, która
akurat wygląda jak liczba/`yes`/`no`/`null`) łatwo coś przypadkiem
popsuć. SQLite (kolumna `TEXT`, wartości serializowane jako JSON) nie ma
tego rodzaju niejawnego typowania - nie ma czym tego zepsuć.

**Migracja ze starszej instalacji** jest automatyczna i jednorazowa: jeśli
`config.db` jeszcze nie istnieje, ale `config.yaml` (+ siostrzane pliki
`satel_fields.yaml`/`tuya_devices.yaml`/`automations.yaml`/
`sensors.yaml`) tak, pierwsze uruchomienie (`main.py` albo
`dashboard.app`) importuje je do nowej bazy i zmienia nazwy starych
plików na `*.migrated` (zostają na dysku do wglądu, ale przestają być
czytane - dalsza ręczna edycja `config.yaml` po tym momencie nic już nie
robi).

**Kopia zapasowa (eksport/import .zip)** - furtka bezpieczeństwa, żeby
baza nikogo nie zamykała bez wyjścia, w zakładce ⚙ Ustawienia ->
Połączenia:
- **Eksport**: przycisk "⬇ Eksportuj konfigurację (.zip)" (albo
  `GET /api/config/export-zip`, wymaga zalogowania) - pobiera prawdziwe
  archiwum `.zip` z rekonstrukcją wszystkich dokumentów jako osobne pliki
  `.yaml` w środku (`config.yaml`, `satel_fields.yaml`, `tuya_devices.yaml`,
  `automations.yaml`, `sensors.yaml`) - do backupu, wglądu dowolnym
  programem, migracji na inny serwer.
- **Import**: przycisk "⬆ Importuj konfigurację (.zip)" (albo
  `POST /api/config/import-zip`, plik jako `multipart/form-data`, pole
  `file`) - nadpisuje wskazane dokumenty w bazie, ale dopiero **po
  podwójnym sprawdzeniu**, że archiwum się zgadza i bridge z nim
  wystartuje:
  1. **Składnia** - każdy rozpoznany plik w archiwum musi być poprawnym
     YAML-em reprezentującym słownik.
  2. **"Czy zadziała"** - wynikowa konfiguracja (aktualna zawartość bazy +
     nadpisania z importu) jest budowana na TYMCZASOWEJ kopii bazy i
     przepuszczana przez `BridgeConfig.load()` - dokładnie tę samą ścieżkę
     kodu, którą bridge/panel uruchamiają przy prawdziwym starcie procesu -
     plus jawne sprawdzenie `satel.host`/`satel.port` (pól, bez których
     proces i tak zaraz wywaliłby się `KeyError`-em, a których sama obecność
     sekcji `satel` jeszcze nie gwarantuje). `satel.integration_key` jest
     celowo POMINIĘTY tutaj - jest opcjonalny, patrz niżej.

  Jeśli którekolwiek z tych dwóch sprawdzeń się nie powiedzie, **nic nie
  jest zapisywane** - import jest odrzucany w całości z czytelnym opisem
  błędu, zamiast zapisać część dokumentów, a resztę pominąć (patrz
  `bridge/config_store.py: import_yaml_bundle_validated`). Zmiany połączeń
  (host/port/klucze) po udanym imporcie wciąż wymagają pełnego restartu
  (przycisk "🔄 Zrestartuj bridge i panel teraz", ta sama zakładka).

## Konto Tuya Cloud (Access ID / Access Secret)

Te same dwa poświadczenia (**Access ID** i **Access Secret**) są potrzebne
w trzech niezależnych miejscach tego projektu - import urządzeń w
kreatorze (krok 2, `☁ Import z Tuya Cloud`), integracja kamer Tuya Cloud
(krok 7 kreatora / `tuya.cloud` w `config.yaml`) i wirtualne urządzenie
dla danych z kotła (patrz sekcja niżej) - **jeden projekt w Tuya IoT
Platform obsługuje wszystkie trzy**, nie trzeba zakładać osobnych.
**Kreator wpisuje je tylko raz** - pola w kroku 2 i kroku 7 są
zsynchronizowane na bieżąco (wpisanie w którymkolwiek miejscu wypełnia
drugie), więc nie trzeba przepisywać ich dwa razy. Region wybrany w
kroku 2 jest też używany do wyprowadzenia adresów API/Message Service
zapisywanych do `config.yaml` (patrz "Częsty problem: 0 urządzeń..."
niżej) - nie trzeba go osobno wybierać w kroku 7.

**Gdzie znaleźć, jeśli już masz projekt:** [developer.tuya.com](https://developer.tuya.com)
→ zaloguj się → **Cloud** (menu boczne) → wybierz projekt z listy →
zakładka **Overview** pokazuje `Access ID/Client ID` oraz
`Access Secret/Client Secret` (kliknij "Show" przy Secret).

**Jeśli nie masz jeszcze projektu:**
1. Załóż konto na developer.tuya.com (darmowe, osobne od konta w
   aplikacji Tuya Smart/Smart Life, którego już używasz do włączników).
2. **Cloud → Create Cloud Project** - dowolna nazwa, region jak najbliższy
   Tobie (np. Europa Centralna - musi być spójny z regionem wybieranym
   później przy imporcie/integracji), typ *Smart Home PaaS* albo
   *Smart Home Basic Edition*.
3. Po utworzeniu: zakładka **Devices → Link Tuya App Account** - zeskanuj
   kod QR aplikacją Tuya Smart/Smart Life - to podpina Twoje realne
   urządzenia pod projekt chmurowy (bez tego import zwróci pustą listę).
4. Zakładka **Service API** - upewnij się, że włączone są co najmniej
   *IoT Core* i *Authorization* (zwykle domyślnie tak).
5. Wróć do zakładki **Overview** po Access ID/Secret.

Poświadczenia wpisywane przy imporcie w kreatorze **nie są nigdzie
zapisywane** - używane jednorazowo do pobrania listy urządzeń, potem
zapominane (w przeciwieństwie do `tuya.cloud` w `config.yaml`, gdzie
zostają trwale, jeśli włączysz integrację kamer).

### Import urządzeń i sterowanie przez chmurę (po pierwszej konfiguracji)

Po skonfigurowaniu bridge'a import urządzeń przenosi się do zakładki
**☁ Tuya Cloud** (🧩 Wtyczki → Tuya Cloud) - zapisz tam poświadczenia
(Access ID/Secret/region) RAZ, potem przycisk **☁ Importuj z Tuya Cloud**
pobiera listę urządzeń jednym kliknięciem, bez wpisywania ich ponownie.

Import rozdziela urządzenia automatycznie:
- **Komplet danych** (adres IP znaleziony w sieci LAN + `local_key`
  zwrócony przez Tuya Cloud) → podpowiedź do skonfigurowania w zakładce
  **🔌 Tuya (LAN)** - sterowanie lokalne jest szybsze i nie zależy od
  internetu.
- **Niekompletne dane** (typowe - Tuya Cloud nie zawsze zwraca
  `local_key`, albo urządzenie nie jest w tej samej sieci LAN co bridge)
  → lądują w tabeli "Urządzenia sterowane przez chmurę" na tej samej
  stronie - sterowane przez Tuya Cloud REST API (wolniej, wymaga
  internetu), **jako fallback TYLKO dopóki** dane urządzenie nie zostanie
  ręcznie skonfigurowane w zakładce Tuya (LAN) pod tą samą nazwą -
  wtedy lokalne automatycznie przejmuje sterowanie.

Sterowanie chmurowe jest na start ograniczone do prostych przełączników
on/off (bez ściemniania) - "kod przełącznika" (domyślnie `switch_1`,
pasuje do większości gniazdek/przełączników) można zmienić per urządzenie,
jeśli sterowanie nie działa (sprawdź faktyczny kod w zakładce
**Cloud Debugging** na developer.tuya.com, wybierając swoje urządzenie).

Skan sieci LAN (zakładka Tuya (LAN)) dopasowuje też odwrotnie - jeśli
skanowane urządzenie ma ID znane z wcześniejszego importu Tuya Cloud,
jego nazwa pojawia się automatycznie zamiast gołego ID.

### Częsty problem: import zwraca 0 urządzeń (albo błąd 401 na kamery) mimo poprawnych danych

Najczęstsza przyczyna to **niedopasowanie regionu** - Tuya Cloud wymaga,
żeby wybrany region odpowiadał *Data Center* faktycznie przypisanemu do
Twojego konta w aplikacji Tuya Smart/Smart Life (widoczne w apce:
**Ustawienia konta → Merge Account/Account and Security** - tam pokazany
jest region konta). Wybranie innego regionu w kreatorze niż faktyczny
region konta skutkuje albo pustą listą urządzeń, albo błędem
uwierzytelniania na kanale powiadomień (Pulsar/Message Service) dla
integracji kamer, mimo że Access ID/Secret same w sobie są poprawne.

Sprawdź też:
1. Czy w projekcie wykonano **Devices → Link Tuya App Account** (zeskanowanie
   kodu QR) - bez tego projekt nie "widzi" żadnych urządzeń.
2. Czy w zakładce **Cloud → Twój projekt → Message Service** widoczna jest
   aktywna subskrypcja (potrzebna dla integracji kamer/Pulsar w kroku 7) -
   tam też znajdziesz FAKTYCZNY adres serwera Pulsar dla Twojego projektu,
   przydatny, jeśli region nie jest jednym z podstawowych czterech
   (Europa Centralna/USA Zachodnie/Chiny/Indie - dla pozostałych kreator
   celowo NIE zgaduje adresu, patrz komentarz w `setup.html`).
3. **Kliknij "🐛 Pokaż debug"** w kreatorze przed próbą importu - panel na
   dole pokaże dokładną odpowiedź serwera (kod błędu Tuya, treść komunikatu)
   zamiast tylko "0 urządzeń", co zwykle od razu wskazuje przyczynę.

## Lokalny broker MQTT

Zarówno eBUS (`ebusd`), jak i opcjonalnie Home Assistant potrzebują
brokera MQTT. Jeśli nie masz jeszcze żadnego działającego (np. już
używanego przez inne urządzenia w domu), kreator (krok 3, przełącznik
"Uruchom lokalny broker MQTT dla mnie") skonfiguruje adres na
`127.0.0.1:1883` za Ciebie - **ale sam broker trzeba uruchomić ręcznie**,
jedną z dwóch metod (kreator celowo nie wykonuje poleceń systemowych z
poziomu przeglądarki):

**A) Bezpośrednio na RPi:**
```bash
sudo apt update && sudo apt install -y mosquitto
sudo systemctl enable --now mosquitto
```

**B) W Dockerze** (jeśli tak wdrażasz bridge - patrz sekcja "Uruchomienie
w Dockerze" niżej), profil `broker` w `docker-compose.yml`:
```bash
docker compose --profile broker up -d mosquitto
```

W obu przypadkach broker nasłuchuje na porcie `1883` bez hasła/TLS -
wystarczające w zaufanej sieci domowej (ten sam kompromis co reszta
panelu, patrz sekcja "Bezpieczeństwo panelu"). Sam `ebusd` (fizyczne
podłączenie do magistrali eBUS kotła) konfigurujesz niezależnie od tego
wyboru - patrz sekcja "eBUS / ebusd" niżej.

## eBUS / ebusd (dane z kotła Wolf)

Zainstaluj i skonfiguruj `ebusd` (https://github.com/john30/ebusd) osobno,
z wyjściem po MQTT:

```bash
ebusd --mqtthost=127.0.0.1 --mqttport=1883 --mqtttopic=ebusd \
      -d /dev/ttyUSB0 --scanconfig
```

### Jak znaleźć listę dostępnych pól Twojego kotła

Zanim uzupełnisz `ebus.sensors` w `config.yaml`, sprawdź, jakie pola
faktycznie wykrył ebusd dla Twojego kotła Wolf:

```bash
ebusctl find -d
```

Zwraca listę WSZYSTKICH dostępnych pól razem z ich bieżącymi wartościami,
np.:
```
! bai FlowTemp = 38.38;ok
! bai FlowTempDesired = 38.50
! bai HwcDemand = no
! bai PumpHours = 296
```
Format to `circuit pole = wartość`. Żeby dowiedzieć się, na jakim temacie
MQTT dane pole jest publikowane, sprawdź konfigurację MQTT ebusd (domyślnie
`<mqtttopic>/<circuit>/<pole>`, np. `ebusd/bai/FlowTemp`) albo podsłuchaj
ruch bezpośrednio: `mosquitto_sub -h 127.0.0.1 -t 'ebusd/#' -v`.

Dokładnie to ostatnie (podsłuchanie ruchu MQTT) robi automatycznie
przycisk **"🔍 Rozpocznij nasłuch"** na stronie `/plugins/ebus` -
wygodna podpowiedź w przeglądarce zamiast SSH + `mosquitto_sub`. Nasłuch
działa **w tle do 5 minut** (żądanie wraca od razu, wykryte tematy
dopisują się na bieżąco jako nowe wiersze w miarę wykrywania - nie trzeba
czekać na koniec) - dłuższe okno łapie też pola publikowane rzadziej niż
co kilka sekund; zamknięcie/odświeżenie strony nie przerywa nasłuchu w
tle, można wrócić później albo zatrzymać go od razu przyciskiem
"Zatrzymaj teraz". Wciąż tylko podpowiedź, nie pełna lista - `ebusctl
find -d` daje pełniejszy, ale ręczny obraz.

### Jak wybrać, co dokąd trafia

Dwie równoważne drogi - wybierz wygodniejszą:

**A) Panel webowy** (`/plugins/ebus` - link "⚙ Konfiguruj" na karcie eBUS
w zakładce 🧩 Wtyczki): tabela z listą czujników, gdzie każdy ma
trzy niezależne przełączniki (Historia / MQTT / Tuya) i pole na kod DP.
Można dodawać/usuwać czujniki bez dotykania pliku ręcznie. **Zmiany
działają automatycznie w ciągu ok. 1-2s, bez restartu bridge'a** (patrz
"Zmiany działają od razu - bez restartu Dockera" niżej).

**B) Ręczna edycja `sensors.yaml`** (skopiuj z `sensors.example.yaml`):

```yaml
sensors:
  - name: kociol_temp_zasilania
    label: "Temperatura zasilania"
    mqtt_topic: "ebusd/bai/FlowTemp"
    targets:
      history: true              # widoczny w panelu (wykres + status)
      mqtt_publish: true          # publikowany dalej po MQTT (home/boiler/...)
      tuya_virtual_device: true   # wysyłany też do wirtualnego urządzenia Tuya
      tuya_dp: "101"               # kod DP na tym urządzeniu (patrz sekcja niżej)
```

Obie drogi operują na TYM SAMYM pliku (`sensors.yaml`) - można zacząć od
ręcznej edycji, a potem donastrajać z panelu, albo odwrotnie. Ten plik
jest celowo OSOBNY od `config.yaml` (który zawiera dane połączenia i
poświadczenia) - dzięki temu edytor webowy nigdy nie dotyka i nie
nadpisuje komentarzy ani ustawień w głównym configu.

Nie ma już globalnego przełącznika "tryb mqtt czy tuya" - to, co trafia do
Tuya, MQTT, a co zostaje tylko w lokalnym panelu, ustawiasz **osobno dla
każdego pola z osobna**. Pola tekstowe (np. status palnika: `"heating"` /
`"idle"`) też działają - trafiają do statusu w panelu i mogą iść dalej po
MQTT, ale NIE do wykresu historycznego (wymaga liczby) ani do Tuya, jeśli
DP po stronie Tuya oczekuje typu liczbowego.

### Tryb `--mqttjson` ebusd (kilka pól na jeden temat)

Domyślnie ebusd publikuje **jedno pole na jeden temat MQTT**, czystym
tekstem (np. temat `ebusd/bai/FlowTemp`, treść `"22.5"`) - dokładnie to,
czego oczekuje przykład wyżej. Jeśli jednak uruchomiłeś `ebusd` z opcją
`--mqttjson`, część tematów (zwłaszcza na poziomie CAŁEGO OBWODU, np.
`ebusd/hc1`) publikuje zamiast tego **obiekt JSON z kilkoma polami naraz**,
np.:

```json
{"MixerStatus": {"value": "on"}, "TempFloor": {"value": 21.3}}
```

Bridge rozpoznaje ten format automatycznie i wyciąga liczbę/tekst z
`"value"` zamiast pokazywać surowy JSON jako "wartość" czujnika:
- **temat z JEDNYM polem** (`{"FlowTemp": {"value": 22.5}}`) - działa bez
  żadnej dodatkowej konfiguracji, pole wybiera się samo.
- **temat z KILKOMA polami** (jak `ebusd/hc1` wyżej) - trzeba wskazać,
  które pole wziąć, przez opcjonalne pole **`mqtt_field`** (nazwa panelu:
  "pole JSON") przy czujniku - w kreatorze/panelu to drugie, mniejsze pole
  pod tematem MQTT, w `sensors.yaml`:
  ```yaml
  - name: kociol_temp_podlogowa
    label: "Temperatura podłogowa (obwód 1)"
    mqtt_topic: "ebusd/hc1"
    mqtt_field: "TempFloor"
    targets: { history: true, mqtt_publish: true, tuya_virtual_device: false, tuya_dp: "" }
  ```
  Bez `mqtt_field` na temacie z kilkoma polami czujnik po prostu nie
  dostaje nowych wartości (w logach bridge'a pojawi się czytelne
  ostrzeżenie z listą dostępnych pól w tej wiadomości) - to świadomy wybór
  zamiast zgadywania, które pole miałeś na myśli. Implementacja:
  `EbusMqttListener._parse_payload()` w `bridge/ebus_client.py`.
- **temat z kilkoma polami, gdzie KAŻDE pole ma zarówno `"name"`, jak i
  `"value"`** (typowe dla ebusd przy odczycie CAŁEJ wiadomości/definicji
  na raz, np. `{"0": {"name": "N_A", "value": 5.0}, "1": {"name":
  "TempOutdoor", "value": 17.699}, ...}`) - bez `mqtt_field` bridge NIE
  odrzuca takiej wiadomości jak zwykłego przypadku "kilka pól", tylko
  łączy wszystkie nazwane pola w jeden czytelny tekst, np.
  `"TempOutdoor: 17.699 | TempDhwSet: 10.0"` (pola nazwane `"N_A"` -
  placeholder ebusd dla nieużywanych/nienazwanych slotów - są pomijane).
  Wygodne do podglądu całej wiadomości na raz bez konfigurowania osobnego
  czujnika na każde pole; jeśli zależy Ci na historii/alarmach progowych/
  publikacji do Tuya dla KONKRETNEGO pola z takiej wiadomości, nadal
  ustaw `mqtt_field` na numer tego pola (np. `"1"`) - wtedy zadziała jak
  zwykłe wyciąganie jednej wartości opisane wyżej.

## Rotacja i backup bazy

`bridge_history.db` rosłaby w nieskończoność bez czyszczenia (odczyty z
kotła co kilkadziesiąt sekund, zdarzenia ruchu, itd.). Domyślnie bridge
**sam, automatycznie** czyści dane starsze niż 180 dni raz na dobę
(konfigurowalne, sekcja `maintenance` w `config.yaml`) - zero dodatkowej
konfiguracji potrzebnej "z pudełka".

**Ważne rozróżnienie**: aktywne alerty i oczekujące komendy (jeszcze nie
wykonane przez bridge) NIGDY nie są usuwane niezależnie od wieku -
reprezentują bieżący stan systemu, nie historię. Usuwane są tylko
zakończone/rozwiązane wpisy starsze niż próg.

Po czyszczeniu bridge robi `VACUUM`, żeby faktycznie zwolnić miejsce na
dysku - samo `DELETE` w SQLite nie zmniejsza pliku, tylko oznacza miejsce
jako wolne do ponownego użycia wewnątrz tego samego pliku.

### Backup (domyślnie wyłączony)

```yaml
maintenance:
  backup_enabled: true
  backup_dir: "backups"       # NAJLEPIEJ punkt montowania sieciowego dysku/NAS
  backup_keep_count: 7
```

Backup korzysta z wbudowanego API SQLite (`Connection.backup()`) - bezpieczny
nawet gdy bridge jednocześnie zapisuje do bazy, w przeciwieństwie do zwykłego
`cp`, które mogłoby złapać niespójny stan w trakcie zapisu. **Ważne**: backup
na TĘ SAMĄ kartę SD co baza źródłowa nie chroni przed awarią samej karty -
ustaw `backup_dir` na zamontowany dysk sieciowy/NAS, jeśli backup ma być
realną ochroną, a nie tylko wygodą przy przypadkowym błędzie w danych.

### Alternatywa: cron zamiast wbudowanego mechanizmu

Jeśli wolisz pełną kontrolę nad porą uruchomienia (np. dokładnie w nocy,
niezależnie od tego kiedy wystartował proces bridge'a) albo chcesz to
rozdzielić od głównego procesu:

```yaml
maintenance:
  run_in_process: false   # wyłącz wbudowany mechanizm
```

```bash
# crontab -e
0 3 * * * /home/pi/satel_tuya_bridge/venv/bin/python /home/pi/satel_tuya_bridge/maintenance.py --config /home/pi/satel_tuya_bridge/config.db >> /home/pi/satel_tuya_bridge/maintenance.log 2>&1
```

## Wirtualne urządzenie Tuya dla danych z kotła — czy warto?

Krótko: **jeśli nie musisz koniecznie widzieć temperatury kotła w aplikacji
Tuya, wybierz tryb `mqtt`** w `boiler_output.mode`. Jest prostszy, stabilny,
lokalny i łatwo się integruje z Home Assistant/Node-RED/dashboardem.

Tryb `tuya_virtual_device` działa, ale ma realne ograniczenia:
- wymaga konta deweloperskiego Tuya i ręcznego utworzenia "Virtual Device"
  w Tuya IoT Platform (funkcja pomyślana do testowania integracji, nie
  jako trwałe rozwiązanie smart-home),
- może przestać działać przy zmianach zasad Tuya dla kont deweloperskich,
- kod w `bridge/boiler_output.py` tylko AKTUALIZUJE istniejące urządzenie -
  samo utworzenie robisz ręcznie w panelu Tuya.

Możesz też ustawić `mode: "both"` i mieć oba na raz.

### Jak utworzyć wirtualne urządzenie Tuya (krok po kroku)

1. **Załóż konto na [developer.tuya.com](https://developer.tuya.com)** -
   osobne od konta w aplikacji Tuya Smart/Smart Life, którego już używasz
   do włączników.
2. **Utwórz projekt Cloud Development**: `Cloud > Development > Create
   Cloud Project`. Wybierz typ *Smart Home* i region zgodny z Twoim
   kontem aplikacji (np. Europa Zachodnia). Zapisz **Access ID** i
   **Access Secret** - trafiają do `config.yaml` (`tuya.cloud`).
3. **Powiąż projekt ze swoim kontem w aplikacji**: `Devices > Link Tuya
   App Account`, zeskanuj QR kod aplikacją Tuya Smart/Smart Life. Dzięki
   temu wirtualne urządzenie będzie widoczne w Twojej normalnej aplikacji,
   nie tylko w panelu deweloperskim.
4. **Dodaj wirtualne urządzenie**: `Devices > All Devices > Add Device >
   Add Virtual Device`. Z katalogu *Products on TuyaGo* wybierz najbliższy
   typ czujnika, np. kategorię *Temperature & Humidity Sensor*.
5. **Odczytaj `device_id` i kody DP**: w widoku *Debug Device* znajdziesz
   ID urządzenia oraz listę Data Points (np. `va_temperature`) - to
   wpisujesz przy odpowiednim czujniku w `ebus.sensors[].targets.tuya_dp` w
   `config.yaml`.
6. **Subskrybuj i autoryzuj API "IoT Core"**: `Service API` -> subskrybuj
   *IoT Core* (darmowy tier wystarczy), potem *Authorize* w Project
   Overview - inaczej wywołania z bridge'a zwrócą błąd autoryzacji.
7. **Ustaw `boiler_output.mode: "tuya_virtual_device"` (lub `"both"`)** -
   `TuyaVirtualDevicePublisher` w `bridge/boiler_output.py` zacznie
   wysyłać aktualizacje bez zmian w kodzie.

**Zastrzeżenie**: to oficjalnie funkcja deweloperska/debugowa Tuya, nie w
pełni "produkcyjna". Jeśli po wdrożeniu okaże się niestabilna, tryb `mqtt`
daje ten sam efekt praktyczny (widoczność danych, możliwość automatyzacji)
bez tego ryzyka.

## Panel webowy (historia, filtrowanie, alarmy)

Dane historyczne (odczyty z kotła, log zdarzeń ruchu/światła/kamer, alarmy
progowe) trafiają do lokalnej bazy **SQLite** (`bridge_history.db`,
współdzielonej przez główny proces bridge'a i panel).

Świadomie **nie** użyto InfluxDB/Grafany - te napisane są w Go i nie mają
oficjalnych binarek dla architektury ARMv6 (RPi Zero W/oryginalne Zero).
Panel poniżej daje praktycznie te same możliwości (wykresy historyczne,
filtrowanie po czujniku/typie zdarzenia/zakresie czasu, baner aktywnych
alarmów) - cały render wykresów robi przeglądarka (Chart.js), RPi tylko
serwuje dane z SQLite przez lekki Quart + Hypercorn (ASGI).

Uruchomienie (osobny proces od głównego bridge'a):

```bash
python -m dashboard.app --config config.db --port 8090
```

Panel dostępny pod `http://<adres-rpi>:8090/` - **wymaga zalogowania od
razu, przed zobaczeniem czegokolwiek** (patrz sekcja "Bezpieczeństwo
panelu" niżej). Rozwijane menu **⚙ Ustawienia** w górnym pasku zbiera
**🧩 Wtyczki** (`/plugins` - własna strona KAŻDEJ integracji: host/port/
klucze razem z czujnikami/strefami/urządzeniami, patrz niżej),
**🎬 Automatyzacje** (`/automations` - sceny/reguły ruchu/harmonogramy/
eskalacja/symulacja obecności/lokalizacja, patrz sekcja "Automatyzacje"
niżej) i **⚙ Ogólne** (`/system` - konta administratorskie, logi, restart,
kopia zapasowa).

### Wykres historii - wybór wielu czujników naraz

Przycisk **"Czujniki (N) ▾"** nad wykresem otwiera panel z listą WSZYSTKICH
czujników, które mają choć jeden zapisany odczyt (tylko liczbowe czujniki
eBUS - Satel/Tuya mają na razie tylko status na żywo, bez historii do
wykresu). Lista jest:

- **pogrupowana wg obwodu** - ten sam mechanizm co grupowanie w
  `/plugins/ebus` (drugi segment tematu MQTT, np. `ebusd/bm/...` i
  `ebusd/fha/...` trafiają do osobnych grup "bm"/"fha"),
- **filtrowalna** - pole tekstowe nad listą filtruje po nazwie/etykiecie,
- **wielokrotnego wyboru** - zaznacz kilka czujników naraz, żeby nałożyć
  je na jednym wykresie (np. porównać temperaturę zasilania i powrotu) -
  każdy dostaje inny kolor linii, z osobną legendą.

Wybór zapamiętywany jest tylko w pamięci karty przeglądarki (jak reszta
stanu UI) - znika przy odświeżeniu strony, przy starcie domyślnie
zaznaczony jest pierwszy czujnik na liście.

### Zakładka 🧩 Wtyczki - eBUS, Satel, Tuya bez edycji YAML-a

Każda integracja ma **własną stronę** pod `/plugins/<id>` (link
"⚙ Konfiguruj" z przeglądu `/plugins`, patrz sekcja "Zakładka 🧩 Wtyczki -
jeden przegląd wszystkich integracji" niżej) - host/port/klucze POŁĄCZENIA
i lista czujników/stref/urządzeń tej integracji żyją teraz na TEJ SAMEJ
stronie (osobne sekcje, osobne przyciski zapisu - dawna wspólna zakładka
"⚙ Ustawienia"/"🔌 Połączenia" została rozdzielona, żeby konfiguracja
każdej integracji była w jednym, oczywistym miejscu):

- **`/plugins/ebus`** - sekcja "🔌 Połączenie" (adres/port brokera MQTT
  `ebusd`, temat publikacji stanu kotła) i sekcja "Czujniki" (`sensors.yaml`)
  z przyciskiem **"🔍 Rozpocznij nasłuch"**: nasłuchuje w tle do 5 minut na
  skonfigurowanym brokerze MQTT i dopisuje wiersz dla każdego tematu, na
  który `ebusd` w tym czasie coś opublikował - **to podpowiedź, nie pełna
  lista pól** (rzadziej publikowane pola mogą się nie pojawić za pierwszym
  razem - kliknij ponownie albo dodaj ręcznie, patrz "Jak znaleźć listę
  dostępnych pól" niżej).
- **`/plugins/satel`** - sekcja "🔌 Połączenie" (host/port centrali, klucz
  integracji - opcjonalny, patrz "Panel tylko do odczytu bez klucza
  integracji" niżej - klucz/tryb szyfrowania, tryb wirtualnych wejść IP) i
  sekcja "Strefy, wyjścia, wejścia IP, partycje" (`satel_fields.yaml`) z
  przyciskiem **"🔍 Wykryj aktywne strefy/wyjścia"** (jak w kreatorze):
  protokół integracji Satela nie udostępnia listy WSZYSTKICH skonfigurowanych
  stref/wyjść wraz z nazwami (ograniczenie protokołu, nie da się tego
  obejść), więc przycisk nasłuchuje 30 sekund i notuje numery, które w tym
  czasie FIZYCZNIE zmienią stan (otwórz/zamknij drzwi i okna, wywołaj
  czujki ruchu, załącz wyjście) - dodaje je jako nowe wiersze niżej, nadaj
  im nazwę i zapisz. Strefy, które nie zmienią stanu w tym oknie, trzeba
  dodać ręcznie (numer z DLOADX) - host/port centrali brane są automatycznie
  z zapisanego już `config.yaml` (nie trzeba ich przepisywać z sekcji
  "Połączenie" wyżej). Każda strefa/wyjście ma dodatkowo **opcjonalny
  "rodzaj"** (np. drzwi/okno/ruch/dym/zalanie/wibracja/sabotaż dla wejść,
  syrena/przekaźnik/oświetlenie/brama/ogrzewanie dla wyjść) - czysto
  opisowy, dokłada tylko ikonę i etykietę rodzaju na panelu głównym
  (zielone OK/czerwone naruszone zostaje bez zmian niezależnie od
  wybranego rodzaju).
- **`/plugins/tuya`** - urządzenia lokalne (`tuya_devices.yaml`) z
  przyciskiem **"🔍 Skanuj sieć"**, który wywołuje broadcast UDP w LAN
  (`tinytuya.deviceScan`, ok. 18 sekund) i podpowiada ID + adres IP
  znalezionych urządzeń. **Nie podpowiada `local_key`** - Tuya świadomie
  go nie rozgłasza, trzeba go wyciągnąć osobno (`python -m tinytuya
  wizard`) albo zaimportować razem z poświadczeniami z zakładki
  `/plugins/tuya_cloud`. Sekcja **"🐝 Bramki Zigbee"** w tym samym panelu
  pozwala dodać czujniki/przełączniki Zigbee podpięte pod bramkę Tuya (np.
  Tuya Zigbee Gateway/Hub) - patrz "Urządzenia Zigbee przez bramkę Tuya"
  niżej. Każde skonfigurowane urządzenie od razu dostaje kartę na stronie
  głównej (nie dopiero po pierwszym udanym odpytaniu) - jeśli akurat nie
  odpowiada (zły `local_key`/IP/wersja protokołu, urządzenie offline),
  karta zostaje widoczna z wartością "niedostępne" zamiast całkiem
  zniknąć, więc od razu widać, że urządzenie JEST skonfigurowane, tylko
  coś jest z nim nie tak.

Każde pole zaczyna jako **nieaktywne** (nie ma go w konfiguracji), dopóki
nie uzupełnisz wiersza (po odkryciu albo ręcznie) i nie klikniesz "Zapisz"
dla danej sekcji - odkrycie samo w sobie niczego nie włącza ani nie
zapisuje.

**Usunięcie/zmiana numeru wejścia, wyjścia, urządzenia Tuya czy
sceny/reguły automatyzacji od razu znika ze strony głównej** - przy każdym
starcie bridge'a i po każdym zapisaniu ustawień (bez restartu procesu),
stare wpisy statusu, które nie odpowiadają już żadnemu aktualnie
skonfigurowanemu wejściu/wyjściu/urządzeniu/scenie/regule, są automatycznie
sprzątane z bazy (`HistoryStore.prune_status`). Wcześniej taki "osierocony"
wpis zostawał w bazie na zawsze i strona główna pokazywała więcej
wejść/wyjść/urządzeń, niż faktycznie jest w konfiguracji.

Zakładka **⚙ Ogólne** (`/system`) ma sekcję **"🛠 Restart i kopia
zapasowa"** z przyciskiem **"🔄 Zrestartuj bridge i panel teraz"** - w
odróżnieniu od reszty pól opisanych wyżej (zmiany wchodzą w życie
automatycznie bez restartu), zmiany host/port/kluczy/brokerów MQTT w
sekcjach "🔌 Połączenie" poszczególnych wtyczek wymagają pełnego restartu
procesu (żywe połączenia TCP nie są odtwarzane w locie). Przycisk
restartuje OBA kontenery Dockera (`docker-compose.yml` ma dla obu
`restart: unless-stopped` - proces po prostu kończy się, Docker sam go
podnosi ponownie) - bez potrzeby dostępu do konsoli/SSH. Uruchomienie bez
Dockera (`python main.py` w tle/systemd) NIE ma tego automatycznego
podniesienia - proces po prostu się zakończy, trzeba go wystartować
ręcznie/przez systemd.

Czujniki eBUS są dodatkowo **pogrupowane w tabeli wg drugiego segmentu
tematu MQTT** (np. `ebusd/bm/FlowTemp` i `ebusd/fha/FlowTemp` trafiają do
osobnych grup "bm"/"fha" - dokładny podział zależy od tego, jak `ebusd`
nazywa Twoje obwody, np. `bai`/`bm`/`fha`/`hc1` itd. - mechanizm jest
ogólny, nie hardkodowany na konkretne nazwy). Przycisk "🔍 Wykryj
czujniki" nadaje też od razu czytelną etykietę wyprowadzoną z tematu (np.
`FlowTemp` -> "Flow Temp") zamiast zostawiać ją pustą - stąd każdy nowo
wykryty wiersz ma inną etykietę widoczną od razu, nie tylko ten sam szary
placeholder.

### Urządzenia Zigbee przez bramkę Tuya

Czujniki/przełączniki Zigbee (np. czujki drzwi/okna, PIR, gniazdka Zigbee)
**nie łączą się bezpośrednio z WiFi** - komunikują się przez bramkę (np.
Tuya Zigbee Gateway/Hub, także niektóre routery Wi-Fi z wbudowaną bramką
Zigbee sprzedawane pod marką Tuya/Smart Life). Bridge obsługuje je lokalnie
(bez chmury) tak samo jak zwykłe urządzenia WiFi Tuya, przez tę samą bramkę
- ruch idzie przez jej połączenie TCP (biblioteka `tinytuya`, ten sam
mechanizm co Home Assistant).

**Jak dodać (zakładka 🧩 Wtyczki → 🔌 Urządzenia Tuya, `/plugins/tuya`):**

1. W sekcji **"🐝 Bramki Zigbee"** dodaj samą bramkę - potrzebuje własnego
   `id`/adresu IP/`local_key`, dokładnie tak jak zwykłe urządzenie WiFi
   wyżej (wyciągnij je tym samym `python -m tinytuya wizard` albo skanem
   sieci - bramka JEST urządzeniem WiFi, w odróżnieniu od podpiętych pod
   nią czujników).
2. Dodaj urządzenie w zwykłej sekcji **"Urządzenia Tuya"**, ale zamiast
   adresu IP/`local_key` wpisz w polu **"bramka Zigbee"** nazwę bramki z
   kroku 1 - pola IP/local_key znikają (nie są potrzebne, ruch idzie przez
   bramkę) i pojawia się pole **cid/node_id** (opcjonalne - domyślnie takie
   samo jak ID urządzenia).
3. ID podurządzenia i `node_id` (jeśli różni się od ID) znajdziesz tym
   samym `python -m tinytuya wizard` - dla podurządzeń Zigbee wypisuje je
   jako osobne wpisy z polem `node_id`, oraz `gateway_id` wskazującym na
   bramkę - albo w projekcie na developer.tuya.com (Tuya Cloud, ta sama
   ścieżka co import zwykłych urządzeń, patrz krok 2 kreatora wyżej).

Zapisane podurządzenie działa dalej jak każde inne urządzenie Tuya w tym
projekcie - reguły ruchu, sceny, harmonogramy, symulacja obecności,
publikacja stanu do MQTT/Home Assistant (patrz sekcje wyżej) - nie trzeba
niczego konfigurować inaczej tylko dlatego, że urządzenie jest po Zigbee, a
nie po WiFi.

### Zmiany działają od razu - bez restartu Dockera

Zapis czujników eBUS, pól Satela, urządzeń Tuya (sekcje na stronach
poszczególnych wtyczek, `/plugins/<id>`) albo scen/reguł ruchu/symulacji
obecności (`/automations`) **nie wymaga restartu** bridge'a ani całego
kontenera Docker - panel prosi bridge o przeładowanie konfiguracji w tle
(przez tę samą kolejkę komend co uzbrojenie/rozbrojenie, patrz "Jak działa
uzbrajanie/rozbrajanie technicznie" niżej), a bridge podłapuje zmianę w
ciągu ok. 1-2s. Widać to w komunikacie po zapisie oraz jako zdarzenie
"Konfiguracja przeładowana" w dzienniku zdarzeń panelu głównego.

**Wyjątek: sekcje "🔌 Połączenie"** na stronach `/plugins/satel` i
`/plugins/ebus` (host/port centrali Satel, adres brokera MQTT), oraz
poświadczenia Tuya Cloud/Home Assistant na ich własnych stronach - te
ustawienia dotyczą ŻYWYCH połączeń sieciowych (gniazdo TCP do centrali,
klienci MQTT, Tuya Cloud Pulsar), więc bezpieczne zamknięcie i odtworzenie
ich w locie byłoby dużo bardziej ryzykowne niż warte tego dla ustawień,
które i tak zmienia się rzadko. Zmiana czegokolwiek w tych sekcjach
**wciąż wymaga pełnego restartu** procesu bridge'a - przycisk restartu w
zakładce ⚙ Ogólne (patrz wyżej), albo "Build i rebuild - krok po kroku"
(Docker), albo `sudo systemctl restart satel-tuya-bridge` (systemd).

### Sekcje "🔌 Połączenie" - host/port/klucze po pierwszej konfiguracji

Kreator (`/setup`) jest **celowo zablokowany** po pierwszym zapisie (żeby
przypadkowe ponowne wejście na `/setup` nigdy nie nadpisało ręcznych zmian
w `config.yaml`) - więc host/port centrali Satel, kod użytkownika, adres
brokera MQTT, poświadczenia Tuya Cloud, Home Assistant i tematy MQTT
zmienia się **z panelu**, w sekcji **"🔌 Połączenie"** na stronie
odpowiedniej wtyczki (`/plugins/satel`, `/plugins/ebus`, `/plugins/tuya_cloud`,
`/plugins/home_assistant`). Na stronie Satela - w odróżnieniu od reszty
tej strony - **odczyt też wymaga hasła administratora** (kliknij
"🔒 Wczytaj bieżące ustawienia"), bo dane obejmują kod użytkownika;
formularz nigdy nie pokazuje prawdziwej wartości sekretu z powrotem -
tylko informację, czy jest już ustawiony. Zostaw pole hasła/klucza puste,
żeby go NIE zmieniać.

Zmiany z tych sekcji dotykają **tylko** swoich pól połączenia w
`config.yaml` - reszta pliku (reguły kamer/alarmów, automatyka kotła,
ustawienia konserwacji) zostaje nietknięta.

### Zakładka 🧩 Wtyczki - jeden przegląd wszystkich integracji

`/plugins` zbiera WSZYSTKIE integracje (Satel, Tuya lokalnie/Zigbee/Cloud,
eBUS, Home Assistant, publikację stanu do MQTT, UniFi Protect, symulację
obecności, powiadomienia push) w jednym czytelnym miejscu - każda jako
karta z ikoną, opisem, stanem (Włączona/Wyłączona/Nieskonfigurowana) i
liczbą skonfigurowanych elementów (np. "3 urządzenia").

To **przegląd i szybki przełącznik** - **WSZYSTKIE dziewięć integracji ma
już własną, dedykowaną stronę konfiguracji** pod `/plugins/<id>/` (Satel,
Tuya lokalnie/Zigbee współdzielą jedną stronę `/plugins/tuya`, eBUS, Tuya
Cloud, Home Assistant, publikacja MQTT, UniFi Protect, powiadomienia,
symulacja obecności) - link "⚙ Konfiguruj" w każdej karcie prowadzi tam;
patrz "Struktura projektu" niżej i docstring `plugins/__init__.py`.
**Osiem z dziewięciu integracji** (wszystkie oprócz Tuya Zigbee, które
dzieli połączenie/runtime z Tuya lokalnie i nie ma niezależnego cyklu
życia) ma osobną, zapisaną flagę `enabled`, którą da się przełączyć
bezpośrednio z tego ekranu (bez przechodzenia do innej zakładki) ALBO z
własnej strony wtyczki, w sekcji "⚙ Ustawienia wtyczki" (razem z poziomem
logowania - patrz niżej) - WŁĄCZENIE integracji, która nigdy wcześniej
nie miała podanych wymaganych danych (np. Tuya Cloud bez zapisanego
Access ID, UniFi Protect bez hosta kontrolera), nadal wymaga najpierw
uzupełnienia ich we właściwej zakładce (przełącznik odrzuci taką próbę z
czytelnym błędem).

Sześć przełączalnych integracji (Satel, Tuya lokalnie, eBUS, Tuya Cloud,
Home Assistant, publikacja MQTT, UniFi Protect, powiadomienia - żyją w
dokumencie `config`) **wymaga pełnego restartu** procesu bridge'a, żeby
włączenie/wyłączenie faktycznie zadziałało (widać to jako odznakę "wymaga
restartu" na karcie) - żywe połączenie sieciowe (TCP do centrali, klient
MQTT, Tuya Cloud Pulsar) startuje tylko raz, przy starcie procesu (patrz
`bridge/rules_engine.py: start()`). Symulacja obecności jest wyjątkiem -
żyje w dokumencie `automations` i włącza/wyłącza się na żywo w ciągu ok.
1-2s, bez restartu (patrz "Zmiany działają od razu" wyżej).

### Poziom logowania per wtyczka

Każda wtyczka (na swojej dedykowanej stronie, sekcja "⚙ Ustawienia
wtyczki") ma pole **"Poziom logowania zdarzeń"** (DEBUG/INFO/WARNING/
ERROR/CRITICAL albo "domyślny globalny") - nadpisuje globalny domyślny
poziom (ustawiany w zakładce ⚙ Ustawienia ogólne, patrz niżej) TYLKO dla
loggera tej konkretnej integracji. Przydatne przy diagnozowaniu problemu z
jedną integracją (np. `DEBUG` dla Tuya lokalnie) bez zalewania logów
szczegółami reszty systemu. Działa na żywo, bez restartu procesu (patrz
`bridge/logging_config.py: apply_log_levels`, wywoływane przy starcie i
przy każdym przeładowaniu konfiguracji).

**Interwał odpytywania (Satel i Tuya lokalnie).** Te dwie karty mają
dodatkowo pole "Interwał odpytywania" - jak często bridge SAM dopytuje o
bieżący stan wejść/wyjść (Satel: `satel.status_poll_interval_seconds`,
domyślnie 3s, minimum 2s - zalecany zakres 2-5s) i urządzeń Tuya
(`tuya.status_poll_interval_seconds`, domyślnie 15s, obejmuje też
podurządzenia Zigbee - ta sama pętla). To
osobny mechanizm od broadcastów samej centrali/urządzeń o zmianie stanu -
zaobserwowano na realnej instalacji, że zmiana stanu WYJŚCIA nie zawsze
dociera jako broadcast (w odróżnieniu od wejść), więc samo poleganie na
"centrala/urządzenie samo powie" nie wystarcza do rzetelnie ciągłej
synchronizacji panelu/MQTT. W odróżnieniu od włączania/wyłączania
integracji - **to zawsze działa na żywo, bez restartu** (minimum 3s, żeby
nie zalewać centrali/urządzeń zapytaniami przy pomyłce we wpisanej
wartości).

### Zakładka ⚙ Ustawienia ogólne - konta, logowanie, logi

`/system` zbiera wszystko, co konfiguruje się raz i nie jest przypisane
do żadnej konkretnej wtyczki - konta administratorskie, tryb logowania i
przegląd logów całego systemu. Wymaga zalogowania (jak reszta panelu poza
odczytem stanu).

**Konta użytkowników.** Panel obsługuje **wiele niezależnych kont**
(tabela `users` w `config.db`, patrz `bridge/config_store.py`) - płaski
model uprawnień: każde zalogowane konto ma pełny dostęp do zapisu, nie ma
ról ani ograniczeń per-konto. Z tej sekcji dodasz nowe konto (login +
hasło, minimum 8 znaków), zmienisz hasło dowolnego istniejącego konta
albo usuniesz konto - poza **ostatnim** (panel zawsze musi mieć co
najmniej jedno konto, inaczej nikt nie mógłby się zalogować). Pierwsze
konto powstaje w kreatorze `/setup` przy pierwszym uruchomieniu; starsze
instalacje z pojedynczym hasłem `dashboard.auth.password_hash` migrują
się na tę tabelę automatycznie i jednorazowo przy starcie (patrz
`ConfigStore.migrate_single_admin_to_users`).

**Tryby logowania.** Dwa niezależne ustawienia:
- **Limit nieudanych prób logowania** (`max_failed_attempts`, domyślnie
  5) i **czas blokady** (`lockout_seconds`, domyślnie 300s) - ochrona
  przed próbami odgadnięcia hasła siłowo, per adres IP.
- **Zezwól na Basic Auth** (`allow_basic_auth`, domyślnie włączone) -
  poza logowaniem sesyjnym (formularz `/login`, ciasteczko) panel
  akceptuje też nagłówek `Authorization: Basic` na każdym zapisującym
  endpoincie (przydatne do skryptów/curl) - wyłącz, jeśli chcesz wymusić
  logowanie WYŁĄCZNIE przez `/login`.

**Tokeny API.** Trwały mechanizm dostępu dla integracji skryptowych
(Home Assistant, curl, serwer MCP - patrz "Serwer MCP" niżej), niezależny
od sesji przeglądarki i od Basic Auth - wysyłany jako nagłówek
`Authorization: Bearer <token>` na każdym zapisującym/odczytującym
endpoincie panelu. Token generowany jest jako 32 losowe bajty
(`secrets.token_hex(32)`), a w bazie (tabela `api_tokens`) zapisywany jest
wyłącznie jego hash SHA-256 - **surowy token pokazywany jest tylko raz**,
zaraz po utworzeniu w sekcji "Tokeny API"; jeśli go nie skopiujesz, jedyne
wyjście to usunięcie tokenu i utworzenie nowego. Usunięcie tokenu
unieważnia go natychmiast dla wszystkiego, co go używało. Tokeny nie mają
osobnych ról - działają jak zalogowane konto (pełny dostęp do zapisu),
tak samo jak sesja czy Basic Auth.

Czas trwania sesji (`session_ttl_seconds`) i bieżący czas serwera/strefa
czasowa (`TZ`) są tu pokazane **wyłącznie informacyjnie** - synchronizacja
zegara dzieje się na hoście, nie w kontenerze (patrz "Synchronizacja
czasu" niżej), a długość sesji nie jest jeszcze konfigurowalna z panelu.

**Domyślny poziom logowania** (`logging.default_level`, domyślnie
`INFO`) - poziom loggera-rodzica `bridge` (patrz
`bridge/logging_config.py`), obejmuje rdzeń (silnik reguł, odporność na
awarie) i każdą wtyczkę BEZ własnego, jawnie ustawionego poziomu (patrz
"Poziom logowania per wtyczka" wyżej).

**Przegląd logów.** Wszystkie logi przechwycone z obu procesów (`bridge`
i `dashboard` - osobne komponenty na filtrze) trafiają do tabeli `logs` w
`bridge_history.db` (patrz `bridge/log_capture.py: HistoryLogHandler`,
podłączony do loggera `bridge` przy starcie każdego procesu) - w
odróżnieniu od dziennika zdarzeń na stronie głównej (tylko zdarzenia
domenowe: alarmy, zmiany stanu), to SUROWE logi Pythona ze wszystkich
modułów, przydatne przy diagnozowaniu awarii/wyjątków. Filtry: komponent
(bridge/dashboard), logger (pełna nazwa, np. `bridge.satel`), minimalny
poziom (pokazuje też poważniejsze) i liczba ostatnich godzin - z opcją
auto-odświeżania co 10s. Tabela jest **automatycznie przycinana** do
ostatnich 10 000 wpisów (patrz `KEEP_LOGS` w `bridge/log_capture.py`), co
200 zapisów - nie rośnie bez ograniczeń.

### Publikacja stanu do MQTT (niezależnie od Home Assistant)

Stan wejść/wyjść/stref Satela, urządzeń Tuya i urządzeń HomeWhiz (zmywarki,
pralki itp.) jest publikowany na skonfigurowany broker MQTT pod osobnym
tematem per integracja - **niezależnie od tego, czy Home Assistant jest
włączony** (dotychczas publikacja bez HA działała tylko dla danych z kotła
eBUS, przez `boiler_output.mqtt.topic_prefix`). Domyślne tematy: `satel/...`,
`tuya/...` i `homewhiz/...`, konfigurowalne na stronie
`/plugins/mqtt_publish` albo wprost w `config.yaml`:

```yaml
mqtt_publish:
  enabled: true      # domyślnie true, jeśli ebus.mqtt_host jest ustawiony
  topics:
    satel: satel      # -> satel/input/<numer>/state, satel/output/<numer>/state, satel/partition/<numer>/state
    tuya: tuya         # -> tuya/<nazwa_urządzenia>/state
    homewhiz: homewhiz # -> homewhiz/<nazwa_urządzenia>/state
```

Każde urządzenie HomeWhiz z `homewhiz.appliances` jest też automatycznie
udostępniane jako encja `sensor` w Home Assistant Discovery (zakładka
`/plugins/home_assistant`), analogicznie do czujników kotła.

Temat MQTT dla danych z kotła eBUS (dotychczasowy `boiler_output.mqtt.
topic_prefix`, domyślnie `home/boiler`) jest teraz też edytowalny z tej
samej zakładki, jako "Temat MQTT dla stanu eBUS".

#### Explorer - podgląd i ręczna publikacja własnych tematów

Ta sama zakładka `/plugins/mqtt_publish` ma sekcję "🔍 Explorer" -
odświeżaną co 3s tabelę ostatniej znanej wartości każdego tematu, na
którym bridge coś opublikował albo odebrał: Home Assistant Discovery,
tematy `mqtt_publish` (satel/tuya/homewhiz), temat komend HA dla
sterowalnych encji, oraz dane z kotła eBUS. **To celowo NIE jest podgląd
całego brokera** (jak samodzielny MQTT Explorer) - żeby nie trzymać w
pamięci stanu setek tematów niezwiązanych z tym projektem, zwłaszcza na
Raspberry Pi Zero. Dane trafiają do tabeli `status` (`bridge_history.db`,
kategoria `mqtt_topic`) w momencie publikacji/odbioru - bez dodatkowego
połączenia z brokerem.

Sekcja "Ręczna publikacja" pozwala opublikować dowolny payload, ale
**wyłącznie w obrębie własnych prefixów** bridge'a (te same, które widać w
Explorerze - `home_assistant.discovery_prefix`/`state_topic_prefix`,
`mqtt_publish.topics.*`, `boiler_output.mqtt.topic_prefix`) - próba
publikacji poza nimi kończy się błędem 400 zamiast wysłania czegokolwiek.
Przydatne do testowania automatyzacji/integracji bez czekania na
prawdziwą zmianę stanu (np. ręcznie ustaw `tuya/gniazdko/state` na `ON`,
żeby sprawdzić regułę reagującą na ten temat). Publikacja - jak każda
zmiana stanu - przechodzi przez tę samą kolejkę komend co reszta panelu
(`history.enqueue_command("mqtt_publish", ...)`, wykonywana przez proces
`bridge`), więc wymaga zalogowania.

### Alarmy progowe

Reguły w `config.yaml` -> `alarm_rules` pozwalają zdefiniować progi (np.
temperatura zasilania kotła powyżej 85°C) oraz wykrywanie braku danych
(np. moduł eBUS przestał odpowiadać przez 10 minut). Aktywny alarm pojawia
się jako czerwony baner w panelu i - jeśli skonfigurujesz
`notifications.ntfy_topic_url` - jako powiadomienie push przez
[ntfy.sh](https://ntfy.sh) (darmowe, można też self-hostować).

### Bezpieczeństwo panelu - logowanie wymagane dla wszystkiego

**Przed zalogowaniem nic nie jest widoczne** - żaden status, żadna
historia, żadne ustawienia, żadne sterowanie. Jedyne, co działa bez
zalogowania, to sama mechanika logowania (`/login`), kreator pierwszej
konfiguracji (`/setup` - nie ma jeszcze żadnego konta, więc nie da się go
zgate'ować logowaniem), statyka (CSS/JS/logo/favicon - potrzebne, żeby w
ogóle wyrenderować stronę logowania) i healthcheck (`/healthz` -
zewnętrzny monitoring typu uptime-kuma/systemd nie wysyła poświadczeń
panelu). Dotyczy to WSZYSTKIEGO innego, łącznie z samym **podglądem**
strony głównej (status na żywo, historia, dziennik zdarzeń, alarmy
progowe) - wcześniej ta strona była celowo publiczna (tylko podgląd, bez
ustawień/sterowania), na wyraźną prośbę zniesione na rzecz zasady "nic
przed zalogowaniem".

Implementacja: `dashboard/app.py:
_require_login_for_settings_and_control` - jeden globalny hook
(`before_request`) z jawną listą tego, co WOLNO zobaczyć bez logowania
(wymienioną wyżej) - wszystko inne domyślnie chronione, także przyszłe
endpointy dodane w nowych wtyczkach, bez pamiętania o dopisaniu ich do
jakiejkolwiek listy. Próba wejścia na chronioną STRONĘ bez sesji
przekierowuje na `/login?next=...`; chronione API zwraca `401`.

#### Konfiguracja konta administratora (nazwa użytkownika + hasło)

Ustawiane normalnie RAZ w kreatorze (`/setup`, krok "Hasło administratora
panelu") - nazwa użytkownika (domyślnie `admin`, dowolna) i hasło (min. 8
znaków). Ręcznie (bez kreatora, przez import YAML - patrz "Konfiguracja w
bazie danych" wyżej):

```bash
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('TWOJE_HASLO'))"
```

Wynik wklej jako `dashboard.auth.password_hash`, nazwę użytkownika jako
`dashboard.auth.username` (jawnym tekstem - to NIE jest sekret, tylko
identyfikator, jak login, nie hasło) w wyeksportowanym `config.yaml` i
zaimportuj z powrotem (`⬇ Eksportuj konfigurację` -> edytuj plik ->
`⬆ Importuj konfigurację`, zakładka ⚙ Ogólne, sekcja "🛠 Restart i kopia
zapasowa"). Dopóki
`password_hash` jest puste, akcje zmieniające stan są **zablokowane** (nie
ma trybu "otwartego na wszystko" przez pomyłkę - brak hasła = brak
zapisu). `username` bez ustawionej wartości domyślnie to `admin` (dotyczy
też configów sprzed dodania tego pola - aktualizacja pakietu nikogo nie
wylogowuje/blokuje).

**Uczciwie o obecnym braku**: zmiana nazwy użytkownika/hasła PO pierwszej
konfiguracji wymaga eksportu/importu YAML (jak wyżej) i restartu panelu -
w przeciwieństwie do reszty ustawień (Satel/
Tuya/eBUS) - żadna z sekcji "🔌 Połączenie" świadomie NIE oferuje do tego
formularza (zmiana własnego hasła administratora bez potwierdzenia
bieżącego hasła to
osobna, większa funkcja bezpieczeństwa - do zrobienia w kolejnej rundzie,
jeśli będzie potrzebna).

Ochrona przed brute-force: po `max_failed_attempts` (domyślnie 5)
nieudanych próbach z tego samego adresu IP, dalsze próby są odrzucane
przez `lockout_seconds` (domyślnie 300s) - **nawet z poprawnym hasłem**,
dopóki blokada nie wygaśnie. Sprawdzone testem.

#### Jak to działa w przeglądarce - logowanie sesyjne (`/login`)

Prawdziwe logowanie, nie okienko `prompt()`: strona `/login` ma pole nazwy
użytkownika (podpowiedziane automatycznie - nie jest sekretem) i
zamaskowane pole hasła (`<input type=password>`, jak wszędzie indziej w
sieci) - wpisujesz dane logowania RAZ, serwer wystawia w ciasteczku
**`HttpOnly`** (niedostępnym dla JavaScriptu na stronie - ochrona przed
odczytaniem go przez ewentualny złośliwy skrypt/XSS) PODPISANY token sesji.
Dalsze żądania z panelu **nie wysyłają już hasła w ogóle** - tylko ten
token, aż do wylogowania (link "🔓 Wyloguj" w nawigacji każdej strony,
wywołuje `POST /api/logout`) albo wygaśnięcia sesji (12h). Token przetrwa
**restart procesu panelu/bridge'a** (np. po zmianie ustawień połączenia,
która wymaga restartu) - nie trzeba logować się ponownie po każdym takim
restarcie, bo cała informacja o sesji siedzi w samym (podpisanym) tokenie,
a klucz podpisujący jest trwale zapisany w tej samej bazie co reszta
configu. Zmiana hasła konta od razu unieważnia wszystkie jego sesje.

Próba akcji wymagającej logowania bez ważnej sesji (np. wejście prosto na
`/plugins/satel` i kliknięcie "Zapisz") przekierowuje na `/login?next=...` -
po zalogowaniu wracasz dokładnie tam, gdzie byłeś. HTTP Basic Auth
(nagłówek `Authorization: Basic ...`) wciąż działa jako alternatywa **dla
skryptów/automatyzacji poza przeglądarką** (np. `curl` z crona) - panel
webowy go już nie używa.

#### Jak działa uzbrajanie/rozbrajanie technicznie

Panel webowy to **osobny proces** od głównego bridge'a (bez dostępu do
żywego połączenia z Satelem) - polecenie "Uzbrój"/"Rozbrój" trafia do
kolejki komend we wspólnej bazie SQLite (tabela `commands`), a bridge
odpytuje ją co ~1.5s i wykonuje. Panel pokazuje status na żywo
("⏳ zlecono" -> "✅ wykonano"/"❌ błąd"), odpytując wynik komendy.

#### Ważne ograniczenie: to nie jest szyfrowanie transmisji

Ciasteczko sesji (i HTTP Basic Auth, patrz wyżej) przez zwykłe (nie-HTTPS)
połączenie **nie jest szyfrowane w transmisji** - ktoś podsłuchujący ruch
w tej samej sieci mógłby je przechwycić. Sesja jest jednak **wyraźnie
lepsza** niż wysyłanie samego hasła (albo jego zakodowanej postaci) przy
każdym żądaniu: to losowy token bez matematycznego związku z hasłem,
ograniczony czasowo i odwoływalny przez wylogowanie - jego przechwycenie
NIE ujawnia hasła administratora. W zaufanej sieci domowej to akceptowalne
ryzyko; jeśli wystawiasz panel poza LAN (np. przez przekierowanie portów
na routerze), postaw przed nim reverse proxy z HTTPS (Caddy/nginx) albo
używaj VPN (WireGuard/Tailscale) zamiast wystawiać port bezpośrednio.

#### A komunikacja z centralą Satel - czy jest szyfrowana kluczem?

**Opcjonalnie tak**, od migracji na `satel-integra>=1.4.0`. Są tu DWA
zupełnie różne klucze, celowo osobne pola w konfiguracji - nie mylić ich:

- **`satel.integration_key`** (ten sam, który wpisujesz w kreatorze w
  kroku 1) - **hasło protokołu integracji** (kod użytkownika DLOADX/GUARDX).
  Autoryzuje polecenia (uzbrojenie/rozbrojenie, sterowanie wyjściem),
  wysyłane przy KAŻDEJ komendzie. To NIE jest klucz szyfrowania transmisji.
  **Opcjonalne** - odczyt stref/wyjść/partycji nie wymaga uwierzytelnienia
  w protokole integracji, więc bez tego pola kreator i tak da się ukończyć,
  a panel działa w trybie **tylko do odczytu** (`bridge/satel_client.py`:
  `set_output()`/`arm()`/`disarm()` odrzucają wtedy komendę z czytelnym
  komunikatem zamiast wysłać do centrali okaleczoną ramkę - `SatelWriteMessage`
  po prostu POMIJA pusty kod zamiast go dopełnić). Dopisz klucz później w
  sekcji "🔌 Połączenie" na stronie `/plugins/satel`, żeby odblokować
  sterowanie.
- **`satel.encryption_key`** (nowe pole, sekcja "🔌 Połączenie" na stronie
  `/plugins/satel` oraz krok 1 kreatora) - opcjonalny **klucz szyfrowania
  AES** transmisji
  TCP, jeśli centrala/moduł ETHM ma włączoną funkcję "Encrypted connections"
  (ustawianą osobno w DLOADX, jako całkiem inne pole niż hasło integracji).
- **`satel.encryption_enabled`** - JAWNY przełącznik włącz/wyłącz, CELOWO
  osobny od samego faktu zapisania klucza wyżej. Sam zapisany klucz NIE
  wystarczy, żeby szyfrowanie było używane - dopiero gdy ten przełącznik
  jest włączony (checkbox "Centrala ma włączone szyfrowanie transmisji"),
  `bridge/satel_client.py` łączy się przez `SatelEncryptedTransport`
  biblioteki `satel_integra` zamiast domyślnego `SatelPlainTransport`,
  przekazując jej `satel.encryption_key`. Dzięki temu można zachować klucz
  w konfiguracji (np. na wypadek późniejszego włączenia szyfrowania na
  centrali) bez ryzyka, że sam jego zapis przypadkiem przełączy transmisję
  na szyfrowaną. Domyślnie wyłączone - komunikacja z centralą (port 7094)
  i wywołania wirtualnych wejść IP (port z sekcji `virtual_inputs`) idą
  wtedy czystym tekstem po sieci LAN, jak dotychczas.

Jeśli Twoja centrala nie ma włączonego szyfrowania w DLOADX, zostaw
`satel.encryption_enabled` wyłączone - włączenie go z błędnym/
niepasującym kluczem uniemożliwi połączenie. W praktyce, nawet bez
szyfrowania, w zaufanej, odizolowanej sieci domowej czyste połączenie to
zwykle akceptowalne ryzyko (dokładnie ta sama klasa kompromisu co Basic
Auth panelu wyżej). Jeśli jednak centrala/RPi są w sieci współdzielonej z
niezaufanymi urządzeniami, albo zależy Ci na dodatkowej warstwie ochrony:
- włącz szyfrowanie w DLOADX, wpisz ten sam klucz w `satel.encryption_key`
  i zaznacz `satel.encryption_enabled`,
- odizoluj VLAN-em urządzenia automatyki domowej od reszty sieci,
- **nie** wystawiaj portu centrali (7094) ani portu wejść IP na świat -
  jeśli potrzebujesz zdalnego dostępu, użyj VPN (WireGuard/Tailscale) do
  całej podsieci, a nie przekierowania portu.

#### A uzbrajam/rozbrajam z panelu, nic się nie dzieje na centrali (ale stan wyświetla się poprawnie)

Biblioteka `satel_integra` (zarówno stare 0.3.x, jak i obecne 1.x) sama z
siebie NIE czeka na potwierdzenie z centrali (ramka 0xEF "command result")
przy `arm()`/`disarm()`/`set_output()` - uznaje samo WYSŁANIE bajtów po
sieci za sukces. Błędny `satel.integration_key` albo odrzucenie komendy
przez centralę kończyłoby się więc CISZĄ porażką: w logach/panelu komenda
wyglądałaby na wykonaną, a fizycznie nic by się nie działo - podczas gdy
ODCZYT stanu (broadcast/cykliczny poll) działa poprawnie, bo w ogóle nie
przechodzi przez to potwierdzenie. Dlatego `bridge/satel_client.py`
buduje potwierdzane komendy samodzielnie (`_wait_for_command_result()`,
patrz docstring modułu) i czeka na odpowiedź centrali, rzucając czytelny
błąd (widoczny w statusie komendy w panelu), jeśli centrala odrzuci
polecenie albo nie odpowie w 5s - jeśli dalej nic się nie dzieje, sprawdź
w logach dokładny komunikat błędu.

#### A w logach widzę "Got exception: N bytes read... Most likely the other side has disconnected!" zaraz po "Starting monitor_status loop"

Centrala Satel Integra **obsługuje tylko JEDNEGO klienta integracji
naraz** (port 7094). Ten dokładny wzorzec w logach (udane połączenie TCP,
a zaraz potem zerwanie przy pierwszej wysłanej komendzie) oznacza
najczęściej, że coś INNEGO już zajmuje ten slot:
- DLOADX/GUARDX ma otwarte połączenie do tej samej centrali z innego
  komputera,
- działa więcej niż jedna instancja tego bridge'a jednocześnie (np.
  uruchomiony bare-metal I w Dockerze naraz, albo dwa kontenery po
  nieudanym restarcie),
- inna integracja (np. równolegle testowany Home Assistant) łączy się do
  tej samej centrali.

`bridge/satel_client.py` ma od tej rundy poprawek watchdog wykrywający
dokładnie ten scenariusz (biblioteka inaczej ponawiałaby połączenie w
nieskończoność WEWNĄTRZ siebie, a `system_health` w panelu dalej
pokazywałby "połączono") - w logach zobaczysz wtedy dodatkowo ostrzeżenie
`Satel: centrala zerwała połączenie zaraz po nawiązaniu...` z tą samą
podpowiedzią. Zamknij DLOADX/GUARDX i upewnij się, że działa dokładnie
jedna instancja bridge'a, zanim spróbujesz ponownie.

## Odporność na awarie (reconnect, backoff, health-check)

RPi Zero W zwykle łączy się przez WiFi - chwilowe zerwania sieci, restart
`ebusd`, czasowa niedostępność urządzenia Tuya czy przeciążenie centrali
Satel to codzienność, nie wyjątek. Dlatego każdy komponent, który łączy się
z czymś zewnętrznym, sam się podnosi po awarii:

- **Satel** - `bridge/satel_client.py` (`run_forever()`) automatycznie
  łączy się ponownie z exponential backoff (5s -> 300s max) po każdej
  utracie połączenia. Callbacki (reakcja na wejścia/wyjścia) są zapamiętane
  na obiekcie klienta i podpinane na nowo przy każdym połączeniu.
- **MQTT** (ebusd + publikacja danych kotła) - wykorzystuje wbudowany w
  `paho-mqtt` automatyczny reconnect (`connect_async` + `loop_start` +
  `reconnect_delay_set`), więc bridge nie wywali się, jeśli ebusd jeszcze
  nie wystartował przy uruchomieniu RPi.
- **Tuya lokalnie** - każde wywołanie (włącz/wyłącz/status) ma 2 automatyczne
  ponowienia z krótkim opóźnieniem (`bridge/tuya_local.py`), bo tinytuya
  często nie rzuca wyjątku przy chwilowej niedostępności, tylko zwraca
  słownik z kluczem `Error` - to też jest wykrywane.
- **Kamery Tuya (Pulsar) i UniFi Protect** - opakowane w ten sam mechanizm
  nadzorujący (`bridge/resilience.py: Supervisor`) co Satel.

Wszystkie te komponenty raportują swój status do panelu webowego - sekcja
**"Zdrowie połączeń"** pokazuje na bieżąco, co jest połączone, a co nie
(z czasem ostatniej zmiany). Zdarzenia zmiany stanu (utrata/przywrócenie
połączenia) trafiają też do dziennika zdarzeń jako typ `system`.

### Podgląd na żywo z kamer UniFi Protect

Zakładka `/plugins/unifi_protect` ma sekcję "📷 Podgląd na żywo" - wybór
kamery (lista pobierana z kontrolera przez `uiprotect`) i odświeżany co
ok. 1,5s zrzut (`ProtectApiClient.get_camera_snapshot`). To **celowo NIE
jest pełny strumień wideo** (RTSP/WebRTC) - seria zdjęć JPEG wystarcza do
szybkiego podglądu "co się dzieje teraz", bez FFmpeg, dodatkowych portów
czy zależności do transkodowania wideo w przeglądarce. Odświeżanie
pauzuje się automatycznie, gdy karta przeglądarki jest w tle
(`document.visibilityState`), żeby nie obciążać kontrolera niepotrzebnie,
kiedy nikt nie patrzy.

Połączenie z kontrolerem UniFi Protect na potrzeby podglądu jest osobne od
połączenia procesu `bridge` (używanego do wykrywania ruchu,
`bridge/unifi_protect_events.py`) - dashboard łączy się bezpośrednio,
tymi samymi poświadczeniami z zakładki wyżej, z własnym, leniwie
tworzonym i buforowanym klientem (jedna próba ponowienia ze świeżym
klientem przy błędzie, np. wygasłej sesji).

### Powiadomienia push o utracie/przywróceniu połączenia

Oprócz samego panelu, bridge wysyła **powiadomienie push (ntfy)**, gdy
którekolwiek źródło (Satel, MQTT/eBUS, Tuya lokalnie, kamery) faktycznie
przestaje odpowiadać - ale dopiero po okresie **karencji**
(`notifications.grace_seconds` w `config.yaml`, domyślnie 120s)
nieprzerwanego rozłączenia. To celowe: chwilowe zerwanie WiFi na kilka
sekund jest normalne i nie powinno budzić Cię w nocy powiadomieniem -
dopiero rzeczywista, dłużej trwająca awaria to robi. Gdy połączenie
wraca, dostajesz osobne powiadomienie "online" (natychmiast, bez
czekania na żaden okres karencji).

Przetestowałem to na symulowanych scenariuszach: krótkie zerwanie
(poniżej `grace_seconds`) - zero powiadomień; długa awaria - dokładnie
jedno powiadomienie "offline" i jedno "online" po powrocie, bez spamu
mimo wielu prób ponawiania połączenia w międzyczasie.

Dla urządzeń Tuya sterowanych lokalnie: pojedyncze niedostępne
urządzenie (np. wyjęte z gniazdka) NIE generuje powiadomienia o awarii
całego kanału - dopiero gdy WSZYSTKIE skonfigurowane urządzenia Tuya
przestają odpowiadać jednocześnie, co zwykle oznacza awarię sieci/WiFi,
a nie pojedynczego urządzenia.

Powiadomienia o alarmach progowych (`alarm_rules`, np. zbyt wysoka
temperatura kotła) działają niezależnie od tego mechanizmu i wysyłane są
natychmiast po przekroczeniu progu - patrz sekcja "Alarmy progowe" niżej.

### Health-check dla zewnętrznego monitoringu

```
GET /healthz
```

Zwraca `200` jeśli wszystkie monitorowane komponenty są połączone, `503`
jeśli którykolwiek zgłasza rozłączenie - wygodne do podpięcia pod
zewnętrzny monitoring (np. [Uptime Kuma](https://github.com/louislam/uptime-kuma),
`healthcheck.io`, albo prosty `curl` w skrypcie systemd/cron).

## Home Assistant (MQTT Discovery)

Opcjonalna integracja - włącz `home_assistant.enabled: true` w
`config.yaml`, żeby wszystkie encje **same** pojawiły się w Home
Assistant, bez ręcznego pisania configuration.yaml po stronie HA.

**Co się pojawia:**
- Czujniki kotła (`sensor`) - tylko te, które mają `targets.mqtt_publish:
  true` w `sensors.yaml` (bo tylko takie faktycznie trafiają na MQTT).
  Czujniki z "temp" w nazwie/etykiecie dostają automatycznie
  `device_class: temperature` i jednostkę °C.
- Urządzenia Tuya (`switch`) - **sterowalne** z poziomu HA (i przez to
  Google Home/Alexa podpięte pod HA).
- Wejścia Satela (`binary_sensor`, `device_class: motion`) - tylko odczyt.
- Wyjścia Satela (`switch`) - sterowalne (to przekaźnik, podobny poziom
  ryzyka co włącznik Tuya).
- Strefy alarmowe (`binary_sensor`, `device_class: safety`) - **CELOWO
  tylko odczyt** ("uzbrojona: tak/nie"), nie `alarm_control_panel`.

### Dlaczego nie da się uzbroić/rozbroić z Home Assistant przez MQTT

To świadoma decyzja bezpieczeństwa, nie przeoczenie. HA MQTT nie ma
wbudowanego mechanizmu autoryzacji per-żądanie jak nasz panel webowy
(`/api/satel/arm`, chronione hasłem - patrz sekcja "Bezpieczeństwo panelu"
wyżej) - gdyby strefa była sterowalną encją MQTT, KAŻDY z dostępem do
brokera MQTT mógłby uzbroić/rozbroić alarm, całkowicie omijając hasło.

Jeśli chcesz sterować alarmem z poziomu Home Assistant (np. przez
automatyzację albo Google Home "uzbrój dom"), zrób to przez **HA
`rest_command`**, który wywoła nasz uwierzytelniony endpoint:

```yaml
# configuration.yaml w Home Assistant
rest_command:
  uzbroj_dom:
    url: "http://<adres-rpi>:8090/api/satel/arm"
    method: POST
    headers:
      Authorization: "Basic {{ 'twoje_haslo' | base64_encode }}"  # a raczej: użyj HA Secret, nie wpisuj hasła wprost w YAML
    payload: '{"partitions": [1]}'
    content_type: "application/json"
```

Dzięki temu autoryzacja zostaje jedna, spójna, i w jednym miejscu -
zamiast dwóch równoległych ścieżek sterowania o różnym poziomie
zabezpieczeń.

## Serwer MCP (Model Context Protocol)

Opcjonalna wtyczka (zakładka 🧩 Wtyczki -> "🤖 Serwer MCP"), która
udostępnia sterowanie systemem jako zestaw narzędzi
[MCP](https://modelcontextprotocol.io/) dla asystentów AI (Claude
Desktop, Claude Code, inne klienty MCP obsługujące zdalny serwer przez
HTTP) - jeden endpoint, `POST /api/mcp`, protokół JSON-RPC 2.0
("Streamable HTTP", bez zależności od pakietu `mcp` - obsługa napisana
ręcznie w `plugins/mcp_server/routes.py`, żeby nie ciągnąć do projektu
Starlette/uvicorn/anyio obok już używanego Quarta/Hypercorna).

**Domyślnie wyłączony** - to pełnoprawny kanał sterowania (uzbrajanie,
urządzenia Tuya, sceny), więc świadomie włącz go w ustawieniach wtyczki,
gdy chcesz podłączyć klienta MCP.

**Autoryzacja - tylko token API.** Endpoint jest chroniony tak samo jak
reszta panelu (sesja/Basic Auth/token - patrz "Zakładka ⚙ Ustawienia
ogólne" wyżej), ale w praktyce klient MCP nie ma przeglądarki ani hasła
pod ręką - użyj **tokenu API** (`Authorization: Bearer <token>`,
wygenerowanego w sekcji "Tokeny API"). Strona ustawień wtyczki pokazuje
gotowy adres endpointu oraz przykładową konfigurację klienta.

**Dostępne narzędzia** - każde wywołanie trafia do tej samej kolejki
komend co REST API (`history.enqueue_command`, wykonywane przez proces
`bridge`, patrz sekcja "Bezpieczeństwo panelu" o architekturze
komend) - MCP nie omija żadnej walidacji ani logiki, jakiej nie miałyby
istniejące endpointy:
- `get_status(category?)` - aktualny stan czujników/wejść/wyjść/urządzeń.
- `list_scenes()` - lista zdefiniowanych scen (nazwa, czy włączona, liczba akcji).
- `run_scene(name)` - uruchamia scenę o podanej nazwie.
- `satel_arm(partitions?, mode?)` / `satel_disarm(partitions?)` -
  uzbraja/rozbraja strefy (domyślnie wszystkie skonfigurowane).
- `trigger_virtual_input(name)` - aktywuje wirtualne wejście Satela.
- `tuya_on(name)` / `tuya_off(name)` - włącza/wyłącza urządzenie Tuya.

Wywołanie narzędzia czeka do 5 sekund na wykonanie komendy przez proces
`bridge` (odpytując `commands.status` co 0,2s) - jeśli bridge nie zdąży w
tym czasie (np. jest właśnie przeładowywany), narzędzie zwraca
`status: "pending"` z `command_id`, zamiast wisieć w nieskończoność.
Nieznana nazwa sceny/urządzenia/wejścia albo błąd wykonania komendy
wraca jako `isError: true` z czytelnym komunikatem - LLM po drugiej
stronie dostaje treść błędu zamiast pustej odpowiedzi.

## Most HomeKit (Apple Home)

Opcjonalna wtyczka (zakładka 🧩 Wtyczki -> "🏡 Most HomeKit") - natywny
most [HomeKit](https://developer.apple.com/homekit/) uruchamiany
wewnątrz procesu `bridge` (biblioteka `pyhap`/`HAP-python`,
`bridge/homekit_bridge.py`), bez pośrednictwa Home Assistant. Po
sparowaniu widoczny bezpośrednio w aplikacji Dom (i przez to sterowalny
przez Siri) - **domyślnie wyłączony**, bo to pełnoprawny kanał
sterowania systemem alarmowym.

### Jak skonfigurować krok po kroku

1. **Sieć hosta w Dockerze - domyślnie już włączona (Linux).**
   `docker-compose.yml` ma `network_mode: host` przy usłudze `bridge`
   ustawione od razu (wymagane przez parowanie HomeKit - ogłoszenia
   mDNS/Bonjour nie przechodzą przez domyślną sieć mostkowaną Dockera).
   Nic nie trzeba zmieniać - chyba że uruchamiasz na Mac/Windows (Docker
   Desktop nie obsługuje `network_mode: host`), wtedy zakomentuj tę linię
   w pliku i pamiętaj, że sparowanie mostu HomeKit nie będzie możliwe.
2. **Włącz wtyczkę.** Zakładka 🧩 Wtyczki -> "🏡 Most HomeKit" ->
   zaznacz "Wtyczka włączona" -> Zapisz. Zmiana wymaga pełnego restartu
   procesu bridge'a (przycisk "Zrestartuj bridge i panel teraz" w
   zakładce ⚙ Ustawienia ogólne -> Połączenia, albo
   `docker compose restart bridge`).
3. **Opcjonalnie dostosuj nazwę/port mostu.** Ta sama strona wtyczki -
   sekcja "Konfiguracja mostu": nazwa widoczna w aplikacji Dom (domyślnie
   "Satel-Tuya-eBUS") i port TCP (domyślnie `51826`, zmień tylko jeśli
   coś innego w sieci już go zajmuje). Też wymaga restartu bridge'a po
   zapisaniu.
4. **Odczytaj kod parowania.** Sekcja "Parowanie" na tej samej stronie
   pokazuje kod w formacie `XXX-XX-XXX`, wygenerowany automatycznie przy
   pierwszym zapisaniu ustawień.
5. **Dodaj most w aplikacji Dom (iPhone/iPad, ta sama sieć Wi-Fi co
   bridge).** Aplikacja Dom -> "+" -> "Dodaj akcesorium" -> "Nie masz
   kodu ani nie możesz zeskanować?" -> wybierz most z listy (nazwa z
   kroku 3) -> wpisz kod z kroku 4 ręcznie. Po chwili pojawią się
   wszystkie partycje Satela, urządzenia Tuya i włączone sceny jako
   osobne akcesoria w jednym pomieszczeniu/moście.
6. **Gdyby trzeba było sparować od nowa** (np. po zmianie sieci albo
   usunięciu mostu z aplikacji Dom przez pomyłkę) - przycisk "Resetuj
   parowanie" na stronie wtyczki, potem restart bridge'a i parowanie od
   kroku 5.

**Co się pojawia:**
- Każda skonfigurowana partycja Satela - jako akcesorium "System
  alarmowy" (uzbrojenie/rozbrojenie + stan "alarm wyzwolony", patrz niżej).
- Każde urządzenie Tuya (`tuya.devices` + `tuya.cloud_devices`) - jako
  Przełącznik (on/off) albo Żarówka z jasnością (`type: dimmer`).
- Każda **włączona** scena - jako Przełącznik, który po włączeniu
  uruchamia scenę i sam wraca do stanu "wyłączony" po ok. 1 sekundzie
  (typowy wzorzec "scena jako przycisk" w mostach HomeKit, bo HAP nie ma
  natywnej koncepcji jednorazowego wyzwalacza).

**Uzbrajanie z aplikacji Dom - dlaczego bezpieczne (w przeciwieństwie do
MQTT).** HomeKit ma wbudowane, obowiązkowe parowanie i szyfrowanie
end-to-end na poziomie protokołu (Apple Authentication Coprocessor /
SRP) - stąd, w odróżnieniu od integracji MQTT (patrz wyżej "Dlaczego nie
da się uzbroić/rozbroić z Home Assistant przez MQTT"), świadomie
udostępniamy tu PEŁNE sterowanie alarmem, nie tylko odczyt.

**Mapowanie trybów uzbrojenia - uproszczone celowo.** HomeKit rozróżnia
"W domu"/"Poza domem"/"Nocny" tryb uzbrojenia (typowe dla amerykańskich
central z wieloma strefami czuwania), ale centrale Satel nie mają
natywnego odpowiednika tego rozróżnienia - każda partycja to po prostu
zestaw stref, uzbrajany/rozbrajany jako całość. Dlatego wszystkie trzy
tryby uzbrojenia w Home App wywołują to samo: pełne uzbrojenie partycji
(`mode: 0`) - rozróżnienie trybów służy tu tylko wygodzie UI Apple, nie
ma żadnego efektu w centrali. Stan "alarm wyzwolony" (czerwona ikona w
Home App) odzwierciedla realny stan `triggered_partition_numbers`
centrali (naruszenie podczas uzbrojenia), nie jest tylko kosmetyczny.

**Parowanie.** Strona ustawień wtyczki pokazuje kod parowania (format
`XXX-XX-XXX`) do ręcznego wpisania w aplikacji Dom ("Dodaj akcesorium" ->
"Nie masz kodu ani nie możesz zeskanować?") - kod jest generowany raz i
zapisany w konfiguracji (nie regenerowany losowo przy każdym restarcie
bridge'a, żeby nie wygasał niespodziewanie), z przyciskiem "Wygeneruj
nowy kod" gdy trzeba. Stan parowania (klucze, lista sparowanych klientów)
jest trwały - plik `homekit_state.json` obok `config.db` w katalogu
danych - i przyciskiem "Resetuj parowanie" na tej samej stronie, gdyby
trzeba było sparować most od nowa.

**Stabilność identyfikatorów (AID).** Każde akcesorium dostaje
deterministyczny numer AID wyliczony z haszu jego klucza (np.
`tuya:gniazdko_salon`), a nie kolejny wolny numer - dzięki temu dodanie
albo usunięcie jednego urządzenia NIE przesuwa numerów pozostałych i nie
wymusza ponownego parowania całego mostu w aplikacji Dom.

**Wymaga Docker `network_mode: host` (Linux) - domyślnie już włączone.**
Parowanie i wykrywanie mostu w aplikacji Dom opiera się na ogłoszeniach
mDNS/Bonjour w sieci LAN, które nie przechodzą przez domyślną sieć
mostkowaną Dockera - dlatego usługa `bridge` w `docker-compose.yml` ma
`network_mode: host` ustawione od razu, bez potrzeby ręcznej edycji. Na
Mac/Windows (Docker Desktop nie obsługuje tego trybu) trzeba tę linię
zakomentować - wtedy reszta integracji działa normalnie, ale most
HomeKit nie da się sparować. Zmiana portu, nazwy mostu, oraz
włączenie/wyłączenie wtyczki wymagają pełnego restartu procesu bridge'a
(jak reszta zmian sieciowych).

## Automatyzacje: sceny, harmonogramy, eskalacja, warunki, łańcuchy, symulacja obecności

Oprócz prostych reguł "ruch -> włącz urządzenie", bridge wspiera bardziej
złożoną automatykę - wszystko zbudowane na jednym wspólnym fundamencie:
**scenach** (`bridge/scenes.py`).

**W pełni konfigurowalne z przeglądarki, bez dotykania YAML-a** - zakładka
**🎬 Automatyzacje** (`/automations`, link z górnego paska panelu) pozwala
dodawać/edytować/usuwać sceny, reguły ruchu (harmonogram + eskalacja +
dodatkowy warunek and/or/not + wyzwalanie łańcuchowe kolejnej reguły, patrz
niżej) i symulację obecności. **Wymaga zalogowania administratora
panelu** (tego samego hasła z kroku "Hasło administratora panelu"
w kreatorze - patrz sekcja "Bezpieczeństwo panelu" niżej) - dotyczy
zarówno przeglądania, jak i zapisu. Alternatywnie:
te same trzy rzeczy da się skonfigurować już **w kreatorze** przy
pierwszym uruchomieniu (kroki "Sceny" i "Reguły ruchu"), albo przez
import YAML (patrz "Konfiguracja w bazie danych" wyżej) - wszystkie trzy
drogi prowadzą do tego samego dokumentu w bazie.

### Gdzie to jest zapisane: dokument `automations`, nie `config`

Sceny/reguły ruchu/symulacja obecności trafiają do **osobnego dokumentu**
`automations` w `config.db` (patrz "Konfiguracja w bazie danych" wyżej) -
z dokładnie tego samego powodu, dla którego czujniki eBUS mają swój
dokument `sensors`: to jedyne dokumenty, które UI (kreator i panel
`/automations`) wolno mu bezpiecznie **nadpisać w całości** przy każdym
zapisie, bez ryzyka skasowania reszty konfiguracji. Kształt danych -
patrz `config.example.yaml` (sekcje `scenes`/`reguły_ruchu`/
`presence_simulation`, wciąż aktualny jako dokumentacja pól, mimo że sam
plik nie jest już bezpośrednio wczytywany) i przykłady niżej.

Ten sam wzorzec (osobny dokument, panel może go bezpiecznie nadpisać w
całości) obowiązuje też dla pól edytowanych w zakładce ⚙ Ustawienia
(patrz sekcja "Panel webowy" wyżej): dokument `satel_fields` dla
stref/wyjść/wejść IP/partycji, dokument `tuya_devices` dla urządzeń Tuya.

Configi sprzed migracji na bazę (gdzie te sekcje były wpisane bezpośrednio
w `config.yaml`) migrują się automatycznie przy pierwszym uruchomieniu -
patrz "Konfiguracja w bazie danych" wyżej.

**Zmiany z panelu/kreatora wchodzą w życie po restarcie procesu bridge'a**
(samego panelu nie trzeba restartować - patrz "Jak działa uzbrajanie/
rozbrajanie technicznie" niżej, ten sam mechanizm dwóch procesów).

### Sceny

Nazwana grupa akcji na wielu urządzeniach naraz, wywoływana jednym
zdarzeniem. Definiujesz w panelu (`/automations`) albo ręcznie w
`automations.yaml`:

```yaml
scenes:
  - name: "wykryto_intruza"
    actions:
      - {type: tuya_on, device: gniazdko_salon}
      - {type: tuya_on, device: swiatlo_korytarz}
      - {type: satel_output, output_number: 1, state: true}
      - {type: delay, seconds: 2}
      - {type: satel_virtual_input, virtual_input: motion_hall_camera}
```

Dostępne typy akcji: `tuya_on`, `tuya_off`, `tuya_on_for_duration`,
`satel_output`, `satel_virtual_input`, `satel_arm`, `satel_disarm`, `delay`.
Reguły ruchu (`reguły_ruchu`) i reguły kamer (`reguły_kamera_alarm`) mogą
wywoływać scenę zamiast pojedynczej akcji - wystarczy dodać pole `scene:
"nazwa"`. Błąd jednej akcji w scenie (np. martwe urządzenie) nie przerywa
pozostałych - sprawdzone testem.

Pole "Jasność %" (`dimmer_level_percent`) w edytorze pojawia się TYLKO po
wybraniu urządzenia skonfigurowanego jako ściemniacz (`tuya.devices[].type:
dimmer`, patrz zakładka 🔌 Tuya) - dla zwykłego gniazdka/przekaźnika nie ma
sensu i tylko myliłoby. Dotyczy zarówno akcji scen (`tuya_on`/
`tuya_on_for_duration`), jak i akcji "Włącz urządzenie na czas" w regule
ruchu niżej.

### Reguła ruchu - edytor podzielony na sekcje: Wejście, Wyjście, Warunek, Harmonogram, Eskalacja

Karta reguły ruchu w `/automations` jest podzielona logicznie na 5 sekcji,
każda w osobnej, podpisanej ramce - **1. Wejście** (co wyzwala regułę:
system + urządzenie źródłowe + wyzwalacz + rodzaj akcji), **2. Wyjście**
(system + urządzenie docelowe + czas), **3. Dodatkowy warunek**,
**4. Harmonogram**, **5. Eskalacja** - patrz sekcje niżej po szczegóły
każdej z nich.

### Wejście reguły ruchu: Satel, albo stan urządzenia Tuya/HomeWhiz (`source`)

Reguła ruchu wyzwala się nie tylko naruszeniem wejścia Satela - może też
reagować na **stan dowolnego urządzenia Tuya albo urządzenia AGD
HomeWhiz** (patrz sekcja "Zmywarki/AGD HomeWhiz" niżej). Pole "System" w
sekcji "1. Wejście" wybiera źródło:

- **Satel (wejście)** (`source: satel_zone`, domyślne) - jak dotąd:
  `zone_number` + `trigger_on` (naruszenie/ustanie).
- **Tuya (stan urządzenia)** (`source: tuya_state`) - `source_device`
  (nazwa urządzenia Tuya) + `trigger_value` (wartość statusu, przy której
  reguła ma zadziałać - lista wyboru: `włączone`/`wyłączone`/`niedostępne`).
- **HomeWhiz (stan urządzenia AGD)** (`source: homewhiz_state`) -
  `source_device` (nazwa urządzenia HomeWhiz) + `trigger_value` (wartość
  statusu, przy której reguła ma zadziałać). Nazwy stanów są specyficzne dla
  modelu urządzenia (np. `finished`/`running`), więc panel `/automations`
  po wybraniu urządzenia źródłowego pobiera na żywo z chmury HomeWhiz listę
  WSZYSTKICH możliwych stanów TEGO KONKRETNEGO modelu (endpoint
  `/api/plugins/homewhiz/appliances/<nazwa>/states`, patrz
  `bridge/homewhiz_client.py: list_possible_states`) i pokazuje ją jako
  listę wyboru - zamiast wolnego tekstu na pamięć. Gdyby pobranie listy się
  nie udało (np. chwilowy brak połączenia z chmurą), pole spada z powrotem
  na wolny tekst.

```yaml
reguły_ruchu:
  # Gdy zmywarka skończy zmywanie - wyślij powiadomienie (przez scenę)
  - source: homewhiz_state
    source_device: "Zmywarka"
    trigger_value: "finished"
    scene: "zmywarka_skonczyla"

  # Gdy czujnik drzwi (Tuya) się otworzy - włącz światło na 2 minuty
  - source: tuya_state
    source_device: "czujnik_drzwi_wejsciowych"
    trigger_value: "włączone"
    target_device: "swiatlo_przedpokoj"
    duration_seconds: 120
```

Reguła wyzwala się DOKŁADNIE wtedy, gdy status urządzenia **zmienia się
NA** `trigger_value` (nie przy każdym odczycie o tej samej wartości -
`RulesEngine.handle_device_state` pamięta ostatnią wartość per urządzenie,
żeby np. pętla odpytywania Tuya co kilkanaście sekund nie odpalała reguły
ponownie, dopóki stan faktycznie się nie zmieni). Tryb "aż do ustania
naruszenia" (puste/zerowe `duration_seconds`, patrz niżej) działa też dla
tych źródeł - urządzenie docelowe włącza się, gdy status STAJE SIĘ
`trigger_value`, i gaśnie, gdy status zmienia się na jakąkolwiek inną
wartość.

### Harmonogramy czasowe (`time_window`)

Dowolna reguła ruchu lub kamery może mieć opcjonalne pole `time_window`,
żeby była aktywna tylko w określonych godzinach:

```yaml
time_window:
  start: "sunset-15"   # 15 minut przed zachodem słońca
  end: "23:30"          # albo stały czas "HH:MM"
```

Obliczenia wschodu/zachodu robi biblioteka `astral` w pełni lokalnie (bez
zewnętrznego API pogodowego) - wymaga sekcji `location` (szerokość/
długość geograficzna) w `config.yaml`. Bez niej `sunset`/`sunrise` po
cichu NIE DZIAŁA - reguła jest wtedy traktowana jako zawsze aktywna
(patrz `bridge/scheduling.py: is_within_window`, komunikat trafia do logu,
nie przerywa działania reszty automatyki). Okna przechodzące przez północ
(np. `22:00`-`06:00`) działają poprawnie - sprawdzone testem na kilku
przypadkach brzegowych.

**Skąd bridge ma znać lokalizację?** Zakładka 🎬 Automatyzacje
(`/automations`), sekcja "🌍 Lokalizacja (wschód/zachód słońca)" - trzy
sposoby wyboru (jak w kreatorze onboardingu Home Assistant): kliknięcie na
interaktywnej mapie (Leaflet + OpenStreetMap, przeciągalny znacznik),
przycisk "📍 Wykryj lokalizację przeglądarki" (`navigator.geolocation` -
przeglądarka najpierw zapyta o pozwolenie), albo ręczne wpisanie
współrzędnych. W ODRÓŻNIENIU od sekcji "🔌 Połączenie" na stronach Satela/
eBUS (host/port/klucze) **zapisuje się na żywo, bez restartu procesu** - lokalizacja to czyste dane, nie żywe połączenie sieciowe do
zerwania (`dashboard/app.py`: `/api/config/location`, `RulesEngine.
reload_config` odświeża `self._location_cfg` i `presence_simulation.
location_cfg` od razu przy każdym przeładowaniu). Wymaga zalogowania jak
reszta ustawień; mapa i tile'e OpenStreetMap wymagają internetu tylko w
momencie wyboru punktu na mapie w przeglądarce - sama praca bridge'a
(obliczanie wschodu/zachodu z już zapisanych współrzędnych) zostaje w
pełni lokalna.

**W panelu (`/automations`) harmonogram jest "wyklikiwany"**, nie wpisywany
ręcznie - wybierasz z listy "Stały czas" / "Zachód słońca" / "Wschód
słońca" i odpowiednio zwykły zegar (`<input type="time">`) albo
przesunięcie w minutach (może być ujemne - "przed"). Panel serializuje to
z powrotem do tego samego formatu tekstowego co wyżej (`"22:00"`/
`"sunset-15"`) - identycznie dla harmonogramu reguły, warunku `time_window`
w edytorze warunków (patrz niżej) i okna symulacji obecności.

### System urządzenia docelowego: Tuya albo wyjście Satela (`target_system`)

"Urządzenie docelowe" akcji reguły ruchu ("Włącz urządzenie na czas"/
"Wyłącz urządzenie") domyślnie odnosi się do urządzenia Tuya
(`target_system: tuya`, też domyślne gdy pole w ogóle nie występuje - stare
configi działają bez zmian). Opcjonalne `target_system: satel_output`
przełącza cel na **wyjście Satela** - wtedy `target_device` jest NAZWĄ
wyjścia (klucz w `satel.monitored_outputs`, zakładka ⚙ Ustawienia -> Satel,
np. brama, syrena, przekaźnik oświetlenia podłączony do centrali), nie
urządzeniem Tuya:

```yaml
reguły_ruchu:
  - source: satel_zone
    zone_number: 7
    target_system: satel_output
    target_device: "Brama garażowa"
    duration_seconds: 15
```

Działa identycznie z każdym trybem akcji opisanym wyżej - stały czas
włączenia, "Wyłącz urządzenie" (`action: turn_off`) i "aż do ustania
naruszenia" (puste `duration_seconds`) - tyle że zamiast
`tuya.turn_on/turn_off/turn_on_for_duration` wywołuje
`satel.set_output(numer, stan)` pod spodem
(`RulesEngine._set_target_state`/`_run_on_for_duration`). Pole "Jasność %"
nie ma zastosowania dla wyjścia Satela (zwykły przekaźnik on/off, bez
ściemniania) - w edytorze znika po wybraniu tego systemu. Nazwa wyjścia,
która zniknęła z configu (np. usunięta/przemianowana w ⚙ Ustawienia),
skutkuje ostrzeżeniem w logu zamiast wywalenia procesu.

W edytorze (`/automations`) wybór jest dwuetapowy - pole **"System"**
(Tuya / Satel - wyjście) obok "Urządzenia docelowego", które podpowiada
listę zależną od wybranego systemu (urządzenia Tuya albo nazwane wyjścia
Satela). Pole "Wejście w Satelu" (numer strefy wyzwalającej regułę) jest z
tego samego powodu listą wyboru zamiast gołego numeru do wpisania na
pamięć - podpowiada nazwane wejścia z `satel.monitored_zones` (zakładka
⚙ Ustawienia -> Satel), dokładnie tak jak wybór urządzenia docelowego wyżej.

### Wirtualne wejścia IP Satela - dwa tryby wyzwalania

"Wirtualne wejście IP" (nazwa w panelu) to sposób wywołania naruszenia w
centrali Satel spoza samej centrali - np. żeby naruszenie czujki Tuya albo
zmiana stanu urządzenia HomeWhiz zgłosiła się jako naruszenie WEJŚCIA w
Satelu (integracja z systemami trzecimi, które rozumieją tylko wejścia
Satela, np. stary panel/centralka monitoringu). Konfigurowane w zakładce
🧩 Wtyczki -> Satel (`satel.virtual_inputs.inputs`), po jednej nazwie na
wejście - patrz `satel.virtual_inputs.mode` w `config.example.yaml` dla
wyboru trybu poniżej.

**Tryb "library" (domyślny, zalecany)** - to NIE jest osobna funkcja
sprzętowa ETHM-1 Plus. To zwykłe WYJŚCIE (numer 1-256, ten sam zakres co
`satel.monitored_outputs`), które instalator zaprogramował w DLOADX tak,
żeby jego załączenie symulowało wewnątrz centrali naruszenie wejścia/czujki.
Wyzwala się je DOKŁADNIE tym samym poleceniem co zwykłe wyjście
(`SatelIntegraClient.set_output`, biblioteka `satel_integra`) - przez TO
SAMO połączenie integracji (port 7094) co reszta poleceń (uzbrojenie,
sterowanie innymi wyjściami), uwierzytelnione wspólnym
`satel.integration_key` - **bez** osobnego portu/klucza per wejście.
Konfiguracja wejścia to więc tylko nazwa + numer wyjścia. Ponieważ centrala
Satel Integra obsługuje TYLKO JEDNEGO klienta integracji naraz i serializuje
na nim wszystkie polecenia zapisu, wyzwolenie wirtualnego wejścia w tym
trybie CZEKA w tej samej kolejce co inne polecenia.

**Tryb "http"** - funkcja "Wejścia IP" modułu ETHM-1: osobne, ZWYKŁE,
NIEUWIERZYTELNIONE żądanie HTTP GET na skonfigurowany port
(`satel.virtual_inputs.port`), `http://{host}:{port}/{nazwa}` -
`{nazwa}` to WPROST nazwa wejścia (fragment ścieżki URL, NIE parametr
zapytania), **BEZ ŻADNEGO klucza/hasła/kodu DLOADX** - dwie wcześniejsze
wersje tego projektu błędnie zakładały najpierw klucz per wejście, potem
błędnie wspólny klucz całego modułu; obie wersje były niepoprawne
(potwierdzone przez użytkownika na prawdziwym sprzęcie) i zostały
usunięte - patrz `SatelVirtualInputTrigger` w `bridge/satel_client.py`.
Idzie przez OSOBNE połączenie, NIEZALEŻNE od kolejki poleceń protokołu
integracji - użyj tego trybu, jeśli wyzwalanie wirtualnych wejść ma
działać bez czekania za innymi poleceniami (np. w trakcie intensywnej
automatyki). Koszt: dodatkowa konfiguracja (osobny port) i zależność od
wersji firmware ETHM-1 (moduł NIE zwraca standardowego kodu HTTP 200 dla
tych zapytań - klient celowo ignoruje kod odpowiedzi i traktuje sam brak
wyjątku sieciowego jako sukces). W tym trybie aktywacja jest z definicji
JEDNORAZOWYM impulsem - protokół nie ma odpowiednika "przywróć".

Tryb i port HTTP wybiera się na stronie `/plugins/satel`, sekcja
"🔌 Połączenie" ("Wirtualne wejścia IP - tryb wyzwalania"), oddzielnie od
samej listy nazwanych wejść (nazwa + numer, TEN SAM kształt niezależnie od
trybu - numer jest po prostu nieużywany w trybie "http") w sekcji "Strefy,
wyjścia, wejścia IP, partycje" tej samej strony.

**Przycisk "TEST"** w sekcji "🧪 Test wyjścia / wejścia wirtualnego IP" (pod
sekcją "🔌 Połączenie" na stronie `/plugins/satel`) pozwala jednorazowo
sprawdzić działanie na żywo: w trybie "library" podaje się numer wyjścia
(używa już zapisanego klucza integracji, bez duplikowania go), w trybie
"http" wybiera się z listy już skonfigurowanych wejść (nazwa) i wysyła
zwykłe żądanie GET - działa od razu po zapisaniu połączenia, bez restartu
procesu bridge'a.

### Wyjście reguły ruchu: aktywacja wirtualnego wejścia IP Satela

Akcja "Wyjście - co się dzieje" reguły ruchu może, zamiast włączać/wyłączać
urządzenie docelowe albo uruchamiać scenę, **aktywować wirtualne wejście
IP** Satela (ten sam mechanizm, którego używa akcja sceny
`satel_virtual_input` oraz przycisk "Aktywuj" na stronie głównej - patrz
"Ręczna aktywacja..." niżej):

```yaml
reguły_ruchu:
  - source: tuya_state
    source_device: "czujnik_drzwi_garazowych"
    trigger_value: "włączone"
    virtual_input: motion_hall_camera
    duration_seconds: 30       # opcjonalne - patrz "Czas włączenia" niżej
```

Pole `virtual_input` (nazwa - klucz w `satel.virtual_inputs.inputs`,
zakładka 🧩 Wtyczki -> Satel) zastępuje `target_device`/`target_system`/
`dimmer_level_percent` - jasność % nie ma tu zastosowania (wejście IP nie
jest ściemniaczem). W edytorze (`/automations`) wybiera się z listy
wyboru "Aktywuj wejście IP Satela" w polu "Akcja".

**Czas włączenia (`duration_seconds`)** działa analogicznie do włączenia
urządzenia (patrz "System urządzenia docelowego" wyżej) - w trybie
"library" to REALNE włączenie/wyłączenie wyjścia (`set_output`), dokładnie
jak dla wyjścia Satela:

- **Puste albo `0`** - "do ustania naruszenia/wyłączenia wyzwalacza":
  reguła włącza wyjście przy "violated" i wyłącza je przy "restored" -
  jedna reguła sama obsługuje obie strony (jak dla urządzenia/wyjścia),
  niezależnie od `trigger_on`.
- **Dodatnia liczba sekund** - włącza wyjście teraz (na wyzwolenie
  wskazane przez `trigger_on`, domyślnie "violated"), potem samo wyłącza
  je po upływie czasu - DOKŁADNIE jedno wyzwolenie źródła uruchamia całą
  sekwencję, tryb "do ustania" jest wtedy wyłączony.

W trybie "http" `duration_seconds` działa analogicznie, ale ponieważ
aktywacja jest jednorazowym impulsem (brak odpowiednika "przywróć" w tym
protokole), "włączenie" i "wyłączenie" to DWA wywołania tego samego żądania
HTTP - jak dwukrotne naciśnięcie tego samego przycisku.

### Ręczna aktywacja wirtualnego wejścia z panelu głównego

Strona główna (`/`) ma osobną kartę "Wirtualne wejścia IP (Satel)" z
przyciskiem "▶ Aktywuj" przy każdym skonfigurowanym wirtualnym wejściu
(`satel.virtual_inputs.inputs`, zakładka 🧩 Wtyczki -> Satel) - do
jednorazowego, ręcznego wywołania niezależnie od automatyzacji (bez stanu
do odczytania - stąd sam przycisk, bez statusu jak przy wejściach/
wyjściach wyżej na tej samej stronie).

Wymaga zalogowania (jak reszta sterowania - uzbrojenie/rozbrojenie). Panel
webowy to osobny proces bez żywego połączenia z Satelem, więc kliknięcie
idzie przez tę samą kolejkę komend w bazie SQLite, którą już wykorzystuje
uzbrojenie/rozbrojenie (`POST /api/satel/virtual-inputs/<nazwa>/trigger` ->
`history.enqueue_command("satel_virtual_input", ...)` -> bridge odbiera i
wykonuje w `RulesEngine._execute_command` w ciągu ok. 1.5s) - przycisk
pokazuje "⏳..." w trakcie oczekiwania i wynik (✅/❌) po wykonaniu.

### Włączanie/wyłączanie urządzeń Tuya z panelu głównego

Karta "Urządzenia Tuya" na stronie głównej pokazuje, obok statusu, przycisk
"⏻ Włącz"/"⏻ Wyłącz" dla każdego urządzenia, którego stan jest znany
(włączone/wyłączone) - urządzenia z nieznanym/niedostępnym stanem (jeszcze
nieodczytane, albo np. czujniki bez faktycznego przekaźnika) przycisku nie
dostają. Działa dla urządzeń lokalnych i chmurowych (Tuya Cloud) - dokładnie
tak samo jak wirtualne wejścia wyżej: przycisk idzie przez kolejkę komend w
bazie (`POST /api/tuya/<nazwa>/on` albo `/off` ->
`history.enqueue_command("tuya_on"/"tuya_off", ...)` -> bridge wykonuje w
`RulesEngine._execute_command`), wymaga zalogowania, pokazuje "⏳..." w
trakcie oczekiwania. Po wykonaniu komendy bridge sam dopytuje o świeży stan
tego jednego urządzenia (`RulesEngine._schedule_tuya_status_refresh`, ok.
1,5s później) zamiast czekać na kolejny pełny cykl odpytywania (domyślnie co
15s) - karta pokazuje więc poprawny przycisk "Wyłącz"/"Włącz" praktycznie od
razu po kliknięciu, a nie dopiero po przeładowaniu statusu.

### Czytelne statusy urządzeń Tuya innych niż włącznik (czujniki, liczniki...)

Urządzenia Tuya, które nie są zwykłym gniazdkiem/przełącznikiem (czujniki
CO2/zalania/wilgotności, kamery, liczniki, sterowniki nawodnienia...) nie
mają jednego pola "włączone/wyłączone" - `RulesEngine._apply_tuya_device_status`
pokazuje wtedy wszystkie pola raportowane przez urządzenie jako czytelny
tekst (`etykieta: wartość | etykieta: wartość`) zamiast samego "?".

- Urządzenia sterowane przez **Tuya Cloud** (zakładka ☁ Tuya Cloud) dostają
  to za darmo - chmura sama zwraca pola z nazwą (`code`, np. `va_temperature`,
  `co2_value`).
- Urządzenia **lokalne** (LAN/Zigbee) znają tylko surowe numery pól (DP) bez
  nazw - dopóki nie pobierzesz ich opisu, karta pokaże surowe
  `numer_pola: wartość`. Przycisk **"🔍 Pobierz opisy pól (Tuya Cloud)"** na
  stronie 🧩 Wtyczki → Tuya (LAN) pobiera dla wszystkich skonfigurowanych
  urządzeń ich opis pól z Tuya Cloud (`devices/{id}/specifications`, wymaga
  zapisanych poświadczeń w zakładce ☁ Tuya Cloud) i zapisuje go trwale przy
  urządzeniu (`dp_schema`) - odtąd karta pokazuje nazwane, przeskalowane
  wartości (np. `temperatura: 23.5C`) zamiast numerów.

### Eskalacja przy powtarzających się naruszeniach

Reguła ruchu z sekcją `escalation` liczy naruszenia w ruchomym oknie
czasowym; po przekroczeniu progu wywołuje MOCNIEJSZĄ scenę zamiast
zwykłej akcji:

```yaml
escalation:
  threshold_count: 3       # 3 naruszenia...
  window_seconds: 120       # ...w ciągu 120 sekund...
  escalated_scene: "wykryto_intruza"   # ...wywołują tę scenę
```

Naruszenia rozłożone w czasie (poza oknem) nie kumulują się - sprawdzone
testem, że pojedyncze sporadyczne ruchy nie wywołują fałszywej eskalacji.

### Dodatkowy warunek reguły (`condition`: and/or/not/state/time_window)

Każda reguła ruchu może mieć opcjonalne pole `condition` - dowolnie
zagnieżdżony warunek logiczny (AND/OR/NOT), analogiczny do `condition:` w
Home Assistant, który MUSI być spełniony, żeby reguła zadziałała (poza
zwykłym dopasowaniem strefy i `time_window` wyżej - te działają zawsze
razem). Dwa typy liścia:

- `state` - porównanie aktualnej wartości DOWOLNEGO statusu widocznego na
  panelu głównym (wejście/wyjście Satela, urządzenie Tuya, czujnik kotła -
  ten sam klucz, co w kolumnie "key" pod `/api/status`, np.
  `satel_input:3`, `tuya:gniazdko_salon`) z zadaną wartością - `equals` albo
  `not_equals`. Ten sam mechanizm obsługuje też **"inna automatyzacja jest
  uruchomiona"** - patrz osobny akapit niżej.
- `time_window` - dokładnie ten sam format co pole `time_window` reguły
  wyżej (w tym `sunset`/`sunrise`), ale jako warunek podrzędny (przydatne
  wewnątrz `not`, żeby wyrazić "poza tym oknem").

```yaml
reguły_ruchu:
  - source: satel_zone
    zone_number: 12
    target_device: gniazdko_salon
    duration_seconds: 60
    condition:
      type: and
      conditions:
        - {type: state, key: "satel_input:9", equals: "naruszone"}   # ktoś jest w przedpokoju
        - type: not
          conditions:
            - {type: time_window, start: "22:00", end: "06:00"}       # ...ale NIE w nocy
```

Edytor w panelu (`/automations`, sekcja "Reguły ruchu") ma do tego
graficzny konstruktor grup AND/OR/NOT z dowolnym zagnieżdżaniem - klucze
statusu wybiera się z listy (to, co panel akurat odczytał), zamiast wpisywać
je ręcznie. **Pole wartości też jest listą wyboru zależną od wybranego
statusu** - dla wejścia/wyjścia Satela, urządzenia Tuya czy strefy panel
sam podpowiada właściwe słowa (`naruszone`/`OK`, `włączone`/`wyłączone`,
`uzbrojona`/`rozbrojona`...), więc nie da się przekręcić literówką wartości,
która i tak nigdy by się nie dopasowała. Dla statusów bez skończonego
zbioru wartości (np. odczyt liczbowy z kotła) zostaje zwykłe pole tekstowe.
Warunek z błędem (np. nieznany typ) jest odrzucany przy zapisie; warunek,
który w trakcie działania nie da się obliczyć (np. odwołuje się do klucza,
który zniknął z configu), traktowany jest jako NIESPEŁNIONY (reguła
bezpiecznie się nie uruchamia, zamiast wywalić cały proces bridge'a) -
sprawdzone testem.

### Warunek "inna automatyzacja jest uruchomiona"

Zwykły warunek `state`, tyle że pod kluczem `automation:<nazwa>` (kategoria
`automation`, wartości `uruchomiona`/`bezczynna`) - bez żadnego osobnego
typu warunku, bo mechanizm statusu (ten sam co dla wejść Satela/urządzeń
Tuya) już wystarcza. Każda **nazwana** scena i **nazwana** reguła ruchu z
akcją "Włącz urządzenie na czas" zapisuje ten status: `uruchomiona` na
czas wykonywania (sceny - cała lista akcji po kolei, włącznie z `delay`;
reguły - cały czas trwania `duration_seconds`), `bezczynna` poza tym.
Akcje natychmiastowe ("Wyłącz urządzenie", wyzwalacz "ustanie naruszenia")
i reguły bez nazwy nie mają czego zaznaczać - nie dają się w ten sposób
odpytać.

```yaml
scenes:
  - name: "podlewanie_trawnika"
    actions:
      - {type: tuya_on_for_duration, device: zawor_ogrod, duration_seconds: 600}

reguły_ruchu:
  - source: satel_zone
    zone_number: 5
    name: "swiatlo_na_taras"
    target_device: lampa_taras
    duration_seconds: 120
    condition:
      type: not
      conditions:
        - {type: state, key: "automation:podlewanie_trawnika", equals: "uruchomiona"}
```

(powyżej: NIE zapalaj światła na tarasie, jeśli akurat leci podlewanie -
np. żeby nie kusić czujki ruchu spryskiwaczem). W edytorze (`/automations`)
klucz `automation:<nazwa>` pojawia się na liście warunku "state" tak samo
jak każdy inny status - dopiero PO PIERWSZYM uruchomieniu bridge'a z tą
sceną/regułą w configu (albo po `reload_config`), z wartością zasianą od
razu jako `bezczynna`, żeby dało się warunek ułożyć zanim cokolwiek
faktycznie się uruchomi. Nazwa sceny i nazwa reguły ruchu muszą być
unikalne WZGLĘDEM SIEBIE (nie tylko w obrębie własnej listy) - obie dzielą
jedną wspólną przestrzeń kluczy `automation:<nazwa>`, więc kolizja
nadpisywałaby sobie nawzajem status; odrzucane przy zapisie.


bezpiecznie się nie uruchamia, zamiast wywalić cały proces bridge'a) -
sprawdzone testem.

### Wyzwalacz reguły: naruszenie czy jego ustanie (`trigger_on`)

Domyślnie reguła ruchu wyzwala się w momencie **naruszenia** czujki
(`trigger_on: violated` - też domyślne, gdy pole w ogóle nie występuje,
więc stare configi działają bez zmian). Opcjonalne `trigger_on: restored`
odwraca to - reguła wyzwala się dopiero, gdy czujka **wraca do stanu OK**
(np. czujnik ruchu przestaje wykrywać ruch, kontraktron drzwi/okna się
zamyka). Przydatne do zgaszenia światła dokładnie wtedy, gdy naruszenie
faktycznie ustało, zamiast zgadywać stały czas w `duration_seconds`:

```yaml
reguły_ruchu:
  - zone_number: 12
    trigger_on: restored
    target_device: swiatlo_korytarz
    action: turn_off
```

`action: turn_off` (dostępne tylko dla akcji na pojedynczym urządzeniu,
nie dla scen) wyłącza `target_device` natychmiast, bez `duration_seconds`/
`dimmer_level_percent` - w przeciwieństwie do domyślnej akcji (włączenie
na czas), która i tak już samodzielnie gasi urządzenie po upływie
`duration_seconds`. W edytorze (`/automations`) odpowiada temu pole
"Wyzwalacz" (Naruszenie czujki / Ustanie naruszenia) obok pola "Akcja",
gdzie doszła nowa opcja "Wyłącz urządzenie".

### Włączenie "aż do ustania naruszenia" (puste albo zerowe `duration_seconds`)

Domyślna akcja "Włącz urządzenie na czas" wymaga stałego czasu
(`duration_seconds`) - urządzenie gaśnie po jego upływie, NIEZALEŻNIE od
tego, czy naruszenie nadal trwa. Jeśli pole "Czas włączenia (s)" w
edytorze zostanie puste ALBO wpisane jawnie jako **0** (albo
`duration_seconds` w ogóle nie występuje w YAML-u - obie wartości znaczą
dokładnie to samo, 0 jest tylko wygodniejszym sposobem wpisania tego w
polu liczbowym), reguła przełącza się w tryb **"aż do ustania
naruszenia"**: urządzenie włącza się przy naruszeniu i gaśnie dokładnie
wtedy, gdy naruszenie ustaje - bez zgadywania czasu, bez osobnej drugiej
reguły z `trigger_on: restored`/`action: turn_off`. Jedna reguła sama
obsługuje obie strony (`RulesEngine._is_until_restored`/
`_rule_matches_trigger` sprawiają, że taka reguła widzi zarówno
"violated", jak i "restored" tego samego wejścia, ignorując pole
`trigger_on` - ono nie ma tu zastosowania, bo reguła i tak reaguje na obie
strony):

```yaml
reguły_ruchu:
  - zone_number: 5
    target_device: swiatlo_korytarz
    # brak duration_seconds (albo duration_seconds: 0) = włącz na
    # naruszenie, wyłącz na jego ustanie
```

Ten tryb działa TAK SAMO dla wejścia "stan urządzenia" (patrz niżej) -
urządzenie włącza się, gdy status STAJE SIĘ wartością wyzwalającą, i gaśnie,
gdy status zmienia się na inną.

`dimmer_level_percent` nadal działa normalnie (ustawia jasność przy
włączeniu). Ten tryb NIE tworzy zadania w tle (`turn_on_for_duration`) -
to zwykłe włączenie/wyłączenie, więc nie pojawia się w statusie
`automation:<nazwa>` (patrz "Warunek 'inna automatyzacja jest
uruchomiona'" wyżej - to zasięganie dotyczy tylko reguł ze STAŁYM czasem
włączenia).

### Wyzwalanie łańcuchowe (`then_trigger_rule`)

Reguła ruchu może mieć nazwę (`name`) i opcjonalne pole
`then_trigger_rule` wskazujące na nazwę INNEJ reguły - po zadziałaniu
(włącznie z eskalacją) ta pierwsza reguła od razu uruchamia drugą,
dokładnie tak, jakby druga zareagowała na własny trigger. Druga reguła
nadal respektuje WŁASNY `time_window`/`condition` - łańcuch tylko ją
wywołuje, nie omija jej warunków.

```yaml
reguły_ruchu:
  - name: wejscie_do_domu
    source: satel_zone
    zone_number: 3
    target_device: swiatlo_przedpokoj
    duration_seconds: 120
    then_trigger_rule: powiadom_o_wejsciu

  - name: powiadom_o_wejsciu
    source: satel_zone
    zone_number: 3            # nieużywane przy wyzwoleniu łańcuchowym -
                               # reguła i tak potrzebuje pasować do jakiegoś
                               # source/zone_number, żeby przejść walidację
    scene: powiadomienie_wejscie
```

Limit głębokości łańcucha (5 poziomów) chroni przed nieskończoną pętlą,
gdyby dwie reguły wskazywały wzajemnie na siebie (`A -> B -> A -> ...`) -
sprawdzone testem, że taki cykl kończy się (z ostrzeżeniem w logu), zamiast
zawiesić proces. Panel (`/automations`) waliduje przy zapisie, że nazwa
docelowa istnieje i że reguła nie wskazuje sama na siebie.

**Zwijane karty reguł w panelu** - przy wielu regułach edytor w
`/automations` łatwo zaśmiecić długimi, rozwiniętymi formularzami. Pole
nazwy reguły siedzi teraz w nagłówku karty (po lewej stronie przycisku
"▼ Zwiń"/"▶ Rozwiń"), więc widać je nawet po zwinięciu. Reguły wczytane z
serwera (już zapisane) startują zwinięte; nowo dodana reguła ("+ Dodaj
regułę") zostaje rozwinięta do edycji, a po kliknięciu "Zapisz
automatyzacje" wszystkie karty zwijają się razem.

### Włączanie/wyłączanie i usuwanie sceny/reguły (`enabled`)

Każda karta sceny i reguły ruchu w edytorze (`/automations`) ma
przełącznik **"Włączona"** w nagłówku (obok przycisku "usuń"). Odznaczenie
go zapisuje `enabled: false` - wyłączona automatyzacja zostaje w configu
(nie trzeba jej usuwać i odtwarzać, żeby tymczasowo ją wstrzymać), ale nie
uruchamia się z ŻADNEGO miejsca, które przez nią przechodzi:

- wyłączona **reguła ruchu** - w ogóle nie jest brana pod uwagę w
  `RulesEngine.handle_motion()` (naruszenie/ustanie naruszenia jej
  przypisanej strefy jest po prostu ignorowane), ani jako cel
  wyzwolenia łańcuchowego (`then_trigger_rule`) - łańcuch zatrzymuje się
  bez błędu, z wpisem w logu.
- wyłączona **scena** - `SceneRunner.run()` (jedyny punkt faktycznego
  wykonania sceny w całym bridge'u) pomija ją bez względu na to, kto ją
  wywołuje: reguła ruchu (`scene:`), eskalacja (`escalated_scene`), reguła
  kamery, automatyka kotła.

Domyślne `enabled: true` (przełącznik zaznaczony) NIE jest zapisywane
jawnie w YAML-u - żeby nie zaśmiecać configu istniejących (starszych)
scen/reguł bez tego pola, bez zmiany zachowania. Karty wyłączonych
automatyzacji są w edytorze wizualnie przyciemnione.

**Usuwanie wymaga potwierdzenia** - przyciski "✕ usuń scenę"/"✕ usuń
regułę" pytają (`confirm()` z nazwą sceny/reguły) przed usunięciem karty z
formularza, żeby przypadkowe kliknięcie nie skasowało całej
skonfigurowanej automatyzacji bez ostrzeżenia (usunięcie z formularza
staje się trwałe dopiero po kliknięciu "Zapisz automatyzacje").

### Symulacja obecności

Losowe włączanie/wyłączanie wybranych urządzeń Tuya w oknie czasowym -
włączasz ręcznie na czas wyjazdu (`presence_simulation.enabled: true`,
przełącznik w panelu `/automations` albo wprost w `automations.yaml`).
Celowo NIEZALEŻNA od stanu uzbrojenia centrali - prostsze i bardziej
przewidywalne niż automatyczne wiązanie z trybem alarmu.

```yaml
presence_simulation:
  enabled: true
  devices: [swiatlo_korytarz, gniazdko_salon]
  window: {start: "sunset", end: "23:30"}   # opcjonalnie - bez okna aktywna cały czas
  min_interval_minutes: 10
  max_interval_minutes: 40
```

### Automatyka wyzwalana danymi z kotła

`boiler_rules` działa jak alarmy progowe, ale zamiast tworzyć alert w
panelu, wywołuje scenę - np. ochrona przed mrozem (bardzo niska
temperatura zewnętrzna -> włącz grzałkę). Ma własny `cooldown_seconds`,
żeby wahania wartości blisko progu nie odpalały sceny co odczyt -
sprawdzone testem.

## Zmywarki/AGD HomeWhiz (BEQ/Arcelik)

Bridge łączy się BEZPOŚREDNIO z chmurą HomeWhiz (Arcelik IoT/AWS) tymi
samymi danymi logowania, co aplikacja HomeWhiz na telefonie - nie ma tu
żadnego pośredniczącego lokalnego brokera (w odróżnieniu od eBUS). Kształt
API rozpoznany na podstawie integracji Home Assistant
([home-assistant-HomeWhiz/home-assistant-HomeWhiz](https://github.com/home-assistant-HomeWhiz/home-assistant-HomeWhiz),
licencja MIT) - to repo NIE jest zależnością tego projektu, tylko źródłem
wiedzy o kształcie żądań/odpowiedzi (`bridge/homewhiz_client.py`).

**Celowo bez `awsiotsdk`/`awscrt`** (skompilowana biblioteka C/Rust, której
używa integracja HA) - zamiast tego zwykłe `paho-mqtt` (transport
WebSocket) + `aiohttp`, już obecne w tym projekcie jako zależności innych
integracji. Podpisywanie żądań (AWS Signature Version 4, wymagane przez
chmurę Arcelik/AWS IoT Core) jest zweryfikowane względem
[oficjalnego zestawu testowego AWS](https://docs.aws.amazon.com/general/latest/gr/signature-v4-test-suite.html)
- patrz `tests/test_homewhiz_client.py`.

### Konfiguracja (zakładka 🧩 Wtyczki -> "🍽 Zmywarki/AGD HomeWhiz")

1. Wpisz login (e-mail) i hasło do konta HomeWhiz i zapisz.
2. Kliknij "🔍 Odkryj urządzenia" - bridge loguje się do chmury i pokazuje
   listę urządzeń przypisanych do konta (zmywarki oznaczone tagiem
   "zmywarka", ale lista pokazuje WSZYSTKIE typy AGD z konta - działa dla
   dowolnego z nich, nie tylko zmywarek). Urządzenia sparowane WYŁĄCZNIE
   przez Bluetooth (bez łączności WiFi) nie są tu widoczne - nie da się ich
   odpytać zdalnie z serwera działającego w Dockerze.
3. Kliknij "+ Dodaj" przy urządzeniu, nadaj mu nazwę (dowolną, unikalną) i
   zapisz listę monitorowanych urządzeń.
4. Włącz wtyczkę (przełącznik u góry strony) - wymaga pełnego restartu
   procesu bridge'a, tak jak inne integracje chmurowe (UniFi Protect, Tuya
   Cloud).

### Stan urządzenia jako warunek ALBO wejście automatyzacji

Zdekodowany stan (np. `running`/`finished`/`idle` - dokładne nazwy zależą
od modelu urządzenia, HomeWhiz sam je definiuje) trafia na stronę główną
jako zwykły status pod kluczem `homewhiz:<nazwa>` - DOKŁADNIE tak samo jak
stan kotła (kategoria `boiler`). Da się go użyć na dwa sposoby:

- **Jako dodatkowy warunek** innej reguły - warunek typu `state`, klucz
  `homewhiz:Zmywarka`, wartość `finished` (patrz "Dodatkowy warunek
  reguły" wyżej) - NIE wymaga żadnego nowego typu warunku, tylko
  istniejący mechanizm statusu.
- **Jako bezpośrednie wejście (wyzwalacz)** reguły ruchu - system
  "HomeWhiz (stan urządzenia AGD)" w sekcji "1. Wejście" edytora
  `/automations` (`source: homewhiz_state`, patrz "Wejście reguły ruchu"
  wyżej) - reguła wyzwala się DOKŁADNIE w momencie, gdy stan zmieni się na
  wybraną wartość, bez potrzeby osobnej reguły źródłowej do podpięcia
  warunku.

W obu przypadkach akcja (np. powiadomienie albo włączenie gniazdka)
uruchomi się dopiero, gdy zmywarka faktycznie skończy zmywanie.

## Synchronizacja czasu

Trzy niezależne miejsca, gdzie "czas" ma znaczenie - żaden nie wymaga
osobnej konfiguracji w kodzie bridge'a, ale warto wiedzieć, gdzie czego
szukać, jeśli coś się "rozjeżdża":

**1. Zegar hosta/kontenera Docker.** Kontenery Dockera na Linuksie **NIE
mają własnego, oddzielonego zegara** - dzielą zegar jądra z hostem
(w odróżnieniu od maszyn wirtualnych), więc poprawny czas w bridge'u
(znaczniki czasu zdarzeń/statusów, harmonogramy, wykresy w panelu) to
wyłącznie kwestia poprawnego zegara **na samym hoście** (RPi/serwerze), nie
czegoś do ustawienia w `docker-compose.yml`. Sprawdź i w razie potrzeby
włącz synchronizację NTP na hoście:
```bash
timedatectl status                          # "System clock synchronized: yes"?
sudo timedatectl set-ntp true                # włącza systemd-timesyncd, jeśli wyłączone
```
Zmienna `TZ=Europe/Warsaw` w `docker-compose.yml` (oba kontenery) to coś
INNEGO - nie synchronizuje zegara, tylko mówi kontenerowi, jak
interpretować "czas lokalny" (harmonogramy `time_window` w automatyzacjach,
strefa czasowa do wyznaczania sunset/sunrise) - zmień ją, jeśli mieszkasz
w innej strefie czasowej.

**2. Zegar samej centrali Satel Integra (RTC).** Bridge **celowo NIE
ustawia** zegara centrali przez protokół integracji - biblioteka
`satel_integra` (i jej odpowiedniki w innych projektach, np. integracja
Home Assistant czy wtyczka openHAB) udostępniają go tylko do ODCZYTU, bez
weryfikowalnej, publicznie udokumentowanej komendy zapisu; wysyłanie
zgadywanych bajtów do żywej centrali alarmowej to zbyt duże ryzyko, żeby to
robić bez pewności. Jeśli zegar centrali się rozjeżdża, ustaw go
bezpośrednio z poziomu klawiatury centrali (menu serwisowe) albo programu
DLOADX/GUARDX - to jedyna sprawdzona droga.

**3. Spójność znaczników czasu MIĘDZY integracjami (Satel/Tuya/eBUS).** Już
teraz spójna - cały bridge to JEDEN proces Pythona, wszystkie zdarzenia i
statusy (baza SQLite, publikacja MQTT) zapisują znacznik czasu tym samym
mechanizmem (`time.time()`, uniksowy epoch w UTC), więc nie ma ryzyka, że
np. zdarzenie z Satela i odczyt z Tuyi w tej samej chwili dostaną różne
"widoki" na czas - różnią się dopiero na etapie WYŚWIETLANIA w przeglądarce
(`toLocaleString`, strefa czasowa/lokalizacja przeglądarki), co jest
zamierzone i normalne.

## Uruchomienie jako usługa systemd (PRZESTARZAŁE)

Dotyczy TYLKO przestarzałej instalacji **bez Dockera** (patrz "Instalacja
bezpośrednio na hoście (bez Dockera) - PRZESTARZAŁE" w sekcji "Instalacja"
wyżej) - przy Dockerze (jedynej oficjalnie wspieranej ścieżce) auto-restart
po awarii/restarcie hosta załatwia `restart: unless-stopped` w
`docker-compose.yml`, ten krok jest zbędny.

```ini
# /etc/systemd/system/satel-tuya-bridge.service
[Unit]
Description=Satel-Tuya-eBUS bridge
After=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/satel_tuya_bridge
ExecStart=/home/pi/satel_tuya_bridge/venv/bin/python main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now satel-tuya-bridge
journalctl -u satel-tuya-bridge -f
```

Analogicznie dla panelu webowego (drugi plik `.service`, np.
`satel-tuya-dashboard.service`, `ExecStart=... python -m dashboard.app --config config.db --port 8090`).

## Uruchomienie w Dockerze

**To jest JEDYNY oficjalnie wspierany sposób uruchomienia** (patrz też
skrócony szybki start w sekcji "Instalacja" wyżej; instalacja bezpośrednio
na hoście istnieje wyłącznie jako przestarzała, nietestowana alternatywa -
patrz "Instalacja bezpośrednio na hoście (bez Dockera) - PRZESTARZAŁE") -
jeden obraz (`Dockerfile`),
budowany **w całości lokalnie** poleceniem `docker compose build`/`up
--build` z plików w tym repo (nic nie jest publikowane ani pobierane z
żadnego prywatnego rejestru - obraz bazowy `python:3.12-slim` pochodzi z
oficjalnego, publicznego Docker Hub, tak jak w praktycznie każdym
Dockerfile). Dwa kontenery z `docker-compose.yml` (bridge + panel
webowy), dzielące wspólny wolumen na dane (`config.db` - cała
konfiguracja, patrz "Konfiguracja w bazie danych" wyżej - plus
`bridge_history.db` i backupy). Działa tak samo na RPi (arm64/armv7) jak
na amd64 - `python:3.12-slim` ma gotowe obrazy dla obu architektur.
Instalacja bezpośrednio na hoście (`venv`/systemd, patrz "Instalacja"
wyżej) zostaje jako alternatywa dla tych, którzy wolą nie używać Dockera.

### Pierwsze uruchomienie

```bash
mkdir -p data
sudo chown 1000:1000 data   # kontener działa jako UID 1000 (nie root)

docker compose up -d --build
```

Otwórz `http://<adres-hosta>:8090/setup` w przeglądarce i przejdź kreator
dokładnie tak samo, jak przy uruchomieniu bez Dockera - `config.db` i
`bridge_history.db` wylądują w `./data/` na hoście (a nie w kontenerze),
więc przetrwają przebudowę/aktualizację obrazu (do backupu/wglądu poza
panelem - patrz "Kopia zapasowa (eksport/import .zip)" wyżej). Po
zapisaniu w kreatorze kontener `bridge` trzeba zrestartować, żeby wczytał
świeżo zapisaną konfigurację:

```bash
docker compose restart bridge
```

### Build i rebuild - krok po kroku

Wszystkie polecenia niżej uruchamiasz z katalogu repo (tam, gdzie jest
`docker-compose.yml`). Rozróżnienie, które ma znaczenie: **zmiana
konfiguracji w `config.db` (przez panel) wymaga tylko restartu** (dane są
w zamontowanym `./data/`, nie w obrazie) - **zmiana w kodzie (`bridge/`,
`dashboard/`, `plugins/`, `requirements.txt`, `Dockerfile`) wymaga
przebudowy obrazu (`--build`)**, inaczej kontener dalej uruchamia STARY kod.

**1. Pierwszy build (raz, przy pierwszej instalacji):**
```bash
mkdir -p data
sudo chown 1000:1000 data
docker compose up -d --build
```
Buduje obraz od zera i uruchamia kontenery w tle (`-d`). Sprawdź, że oba
kontenery żyją:
```bash
docker compose ps
```

**2. Rebuild po aktualizacji kodu (np. `git pull`, zmiana w `bridge/`,
`dashboard/` albo `requirements.txt`):**
```bash
git pull
docker compose up -d --build
```
`--build` przebudowuje obraz TYLKO jeśli coś się zmieniło od ostatniego
builda (Docker cache'uje warstwy) - bezpiecznie odpalać za każdym razem,
nawet jeśli nic się nie zmieniło (wtedy po prostu nic nie robi). `-d`
sprawia, że stare kontenery są podmieniane na nowe automatycznie, bez
osobnego `stop`/`start`.

**3. Restart BEZ rebuildu (wystarczy po zmianie samej konfiguracji w
`config.db`, np. przez panel `/plugins/<id>` albo `/automations` - baza
żyje w `./data/`, nie w obrazie, więc rebuild niczego by tu nie zmienił):**
```bash
docker compose restart bridge
```
Sam panel webowy (`dashboard`) wczytuje zmiany od razu (bez restartu) po
zapisie z UI - restartu wymaga tylko proces `bridge` (osobny proces, patrz
"Co jest w środku" niżej).

**4. Podgląd logów (sprawdzenie, czy wszystko wstało poprawnie po
build/rebuild/restarcie):**
```bash
docker compose logs -f bridge      # logi głównego procesu (Satel, Tuya, eBUS...)
docker compose logs -f dashboard   # logi panelu webowego
docker compose logs -f             # oba naraz
```
(`Ctrl+C`, żeby przestać śledzić - to nie zatrzymuje kontenerów).

**5. Pełny "czysty" rebuild (gdy coś jest wyraźnie nie tak i podejrzewasz
uszkodzony/nieaktualny cache warstw Dockera - rzadko potrzebne):**
```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```
`--no-cache` ignoruje WSZYSTKIE cache'owane warstwy i buduje obraz od
zera (wolniejsze, ale gwarantuje świeżość) - `./data/` (config, baza,
backupy) przetrwa, bo to osobny wolumen, nie część obrazu.

**6. Zatrzymanie (bez usuwania obrazu/wolumenu - dane w `./data/`
zostają):**
```bash
docker compose down
```
(`docker compose down -v` USUNĄŁBY też nazwane wolumeny Dockera - `./data/`
to zwykły bind mount na hosta, więc `-v` go nie kasuje, ale i tak lepiej
tego flagi nie używać bez upewnienia się, co dokładnie robi w Twojej
konfiguracji).

### Co jest w środku

- **`bridge`** - główny proces (`main.py`), bez portów - łączy się
  wychodząco z centralą Satel, urządzeniami Tuya, brokerem MQTT itd.
- **`dashboard`** - panel webowy (`dashboard/app.py`), port `8090`
  wystawiony na hosta, z `healthcheck` opartym o `/healthz`.
- **`mosquitto`** *(opcjonalny, profil `broker`)* - lokalny broker MQTT
  **tylko** jeśli nie masz już takiego, na którym publikuje `ebusd`.
  `ebusd` sam wymaga fizycznego dostępu do adaptera eBUS (USB/szeregowy)
  i **nie jest** tu kontenerowany - zwykle działa bezpośrednio na hoście
  (RPi) albo osobnym urządzeniu; w zakładce ⚙ Ustawienia -> Połączenia
  (`mqtt_host`, eBUS) wskaż adres LAN tego, co faktycznie działa. Włączenie:
  ```bash
  docker compose --profile broker up -d
  ```

### Sieć LAN (Satel, Tuya, HomeKit)

Usługa `bridge` w `docker-compose.yml` domyślnie działa w sieci hosta
(`network_mode: host`, tylko Linux) - wymagane przez wtyczkę "Most
HomeKit" (parowanie z aplikacją Dom opiera się na ogłoszeniach
mDNS/Bonjour, które nie przechodzą przez zwykłą sieć mostkowaną
Dockera), a przy okazji upraszcza discovery po multicaście dla innych
integracji. Połączenia WYCHODZĄCE do centrali Satel i urządzeń Tuya
(skonfigurowanych po adresie IP w panelu) działają tak samo w obu
trybach sieci. Na Mac/Windows (Docker Desktop nie obsługuje
`network_mode: host`) zakomentuj tę linię - reszta integracji nadal
zadziała, tylko mostu HomeKit nie da się sparować.

### Bezpieczeństwo obrazu

Kontener działa jako **użytkownik bez uprawnień roota** (UID 1000) -
stąd wymóg `chown` katalogu `data/` przed pierwszym uruchomieniem
(inaczej Docker utworzy go jako root i kontener nie będzie mógł zapisać
`config.db`). Sekrety (kod użytkownika centrali Satela, local_key urządzeń
Tuya, hasła) trafiają wyłącznie do zamontowanego `./data/config.db` -
nigdy do obrazu ani do warstw builda.

### Ruch sieciowy PODCZAS BUILDA (pobieranie pakietów Python)

Sam **build** obrazu (`docker compose build`/`up -d --build`) łączy się
WYCHODZĄCO z PyPI (`pypi.org`/`files.pythonhosted.org`, serwowane przez
CDN Fastly - stąd adres IP bywa różny w zależności od tego, który węzeł
brzegowy CDN jest najbliżej Twojego łącza) - to `RUN pip install -r
requirements.txt` w `Dockerfile`, zwyczajne i wymagane pobieranie
zależności (PyYAML, aiohttp, tinytuya, satel-integra, paho-mqtt,
uiprotect, tuya-connector-python, Quart, hypercorn, astral). **Sam bridge
w trakcie DZIAŁANIA nie kontaktuje żadnego z tych adresów** - łączy się
tylko z tym, co wpiszesz w panelu (centrala Satel, urządzenia Tuya,
broker MQTT), a opcjonalnie z Tuya Cloud (`openapi.tuya*.com`, tylko
jeśli włączysz integrację Tuya Cloud w zakładce 🧩 Wtyczki) i z serwerem
ntfy (tylko jeśli skonfigurujesz powiadomienia). Jeśli widzisz ruch do
nieznanego adresu IP z kontenera/hosta w trakcie zwykłej pracy (nie
builda) - to NIE jest żaden z modułów tego repo; sprawdź inne procesy na
tej samej maszynie.

`requirements.txt` celowo używa `>=` (nie dokładnie przypiętych wersji,
poza górnym ograniczeniem `satel-integra<2.0.0` i `paho-mqtt<3.0` - patrz
komentarze w pliku, dlaczego), więc **każdy build bez cache'u Dockera i tak pobiera najnowsze
zgodne wersje** z PyPI. Warstwa `RUN pip install` jest jednak
cache'owana przez Docker dopóki `requirements.txt` się nie zmieni - żeby
wymusić świeże pobranie pakietów Python mimo niezmienionego
`requirements.txt` (np. żeby dociągnąć łatkę bezpieczeństwa wydaną od
ostatniego builda), użyj pełnego rebuildu bez cache'u (patrz punkt 5
wyżej):
```bash
docker compose build --no-cache
docker compose up -d
```

## Struktura projektu

```
satel_tuya_bridge/
  config.example.yaml       # dokumentacja pól głównej konfiguracji (połączenia, poświadczenia) -
                             # NIE jest już bezpośrednio wczytywany, patrz "Konfiguracja w bazie danych"
  sensors.example.yaml      # wzór listy czujników eBUS (edytowalny z panelu, patrz /plugins/ebus)
  # config.db - cała faktyczna konfiguracja (dokumenty config/sensors/
  # automations/satel_fields/tuya_devices) - baza SQLite tworzona przez
  # kreator (albo migrowana automatycznie ze starych plików .yaml przy
  # pierwszym uruchomieniu), patrz bridge/config_store.py i sekcja
  # "Konfiguracja w bazie danych"
  requirements.txt
  Dockerfile                # jeden obraz dla bridge'a i panelu, patrz "Uruchomienie w Dockerze"
  docker-compose.yml         # usługi bridge/dashboard/(opcjonalnie) mosquitto
  .dockerignore
  docker/mosquitto.conf      # minimalny config opcjonalnego brokera MQTT (profil "broker")
  main.py                   # punkt wejścia głównego bridge'a
  bridge/
    config.py               # BridgeConfig.load() - czyta z ConfigStore, migruje stare pliki YAML
    config_store.py         # baza SQLite z dokumentami konfiguracji + eksport/import YAML
    satel_client.py         # protokół integracji + wirtualne wejścia IP + test_connection() ("Testuj połączenie")
    tuya_local.py            # sterowanie lokalne (tinytuya) + scan_network()/fetch_cloud_devices() (import w /plugins/tuya)
    tuya_cloud_events.py     # ruch z kamer Tuya (Pulsar, wymaga chmury)
    unifi_protect_events.py  # ruch z kamer UniFi Protect
    homewhiz_client.py        # stan zmywarek/AGD HomeWhiz (SigV4 + MQTT-over-WebSocket do AWS IoT Core)
    ebus_client.py           # odczyt danych z ebusd (MQTT) + discover_topics() (podpowiedź do /plugins/ebus)
    boiler_output.py         # publikacja danych kotła (MQTT / Tuya virtual device)
    history_store.py         # baza SQLite (odczyty + zdarzenia + alarmy + statusy)
    alarms.py                # silnik alarmów progowych + powiadomienia ntfy
    resilience.py            # reconnect z exponential backoff + zdrowie połączeń
    notifier.py               # wspólna wysyłka powiadomień push (ntfy)
    scheduling.py             # okna czasowe (stałe godziny / wschód-zachód słońca)
    scenes.py                 # sceny - grupy akcji na wielu urządzeniach naraz
    presence_simulation.py    # losowe przełączanie urządzeń w oknie czasowym
    boiler_automation.py      # automatyka wyzwalana danymi z kotła (np. ochrona przed mrozem)
    maintenance.py            # rotacja starych danych + backup bazy
    ha_discovery.py            # budowanie encji Home Assistant (MQTT Discovery)
    ha_mqtt.py                 # klient MQTT integracji Home Assistant
    rules_engine.py          # spina wszystko razem wg BridgeConfig.raw
  dashboard/
    app.py                   # backend panelu (Quart + Hypercorn)
    auth.py                   # autoryzacja - tylko akcje zmieniające stan
    setup.py                   # logika kreatora + budowanie dokumentów configu
    templates/index.html     # frontend panelu (Chart.js, filtrowanie, sterowanie alarmem)
    templates/automations.html # edytor scen/reguł ruchu/harmonogramu/eskalacji/symulacji obecności + lokalizacja
    templates/setup.html      # kreator pierwszej konfiguracji (krok po kroku, autozapis postępu)
    templates/plugins.html    # przegląd wszystkich integracji (włącz/wyłącz, interwały)
    templates/system.html     # ⚙ Ustawienia ogólne - konta, logowanie/blokady, poziom logów, przegląd logów, restart + kopia zapasowa
  plugins/                   # wtyczki z WŁASNĄ stroną konfiguracji, jeden podkatalog na integrację -
                              # każda ma sekcję "⚙ Ustawienia wtyczki" (włącznik + poziom logowania,
                              # patrz plugins/__init__.py: register_settings_route) plus własne pola
    satel/routes.py           # /plugins/satel - host/port/klucze (🔌 Połączenie) + strefy/wyjścia/partycje + skanowanie
    tuya_local/routes.py      # /plugins/tuya - urządzenia + bramki Zigbee, skan LAN + import z Tuya Cloud
    ebus/routes.py            # /plugins/ebus - broker MQTT (🔌 Połączenie) + czujniki kotła + wykrywanie tematów MQTT (w tle)
    tuya_cloud/routes.py      # /plugins/tuya_cloud - poświadczenia API (Access ID/Secret, endpointy)
    home_assistant/routes.py  # /plugins/home_assistant - broker MQTT + prefiksy discovery/stanu
    mqtt_publish/routes.py    # /plugins/mqtt_publish - tematy MQTT (Satel/Tuya)
    unifi_protect/routes.py   # /plugins/unifi_protect - host/login/hasło kontrolera UniFi OS
    homewhiz/routes.py        # /plugins/homewhiz - konto HomeWhiz + odkrywanie/wybór monitorowanych urządzeń
    notifications/routes.py   # /plugins/notifications - adres tematu ntfy + okres ciszy
    presence_simulation/routes.py  # /plugins/presence_simulation - włącznik/log_level (lista urządzeń w /automations)
  tests/                     # testy pytest (patrz sekcja "Testy automatyczne")
  .github/workflows/tests.yml  # CI - uruchamia testy na Python 3.12/3.13
  maintenance.py              # CLI do rotacji/backupu bazy (do crona, patrz sekcja "Rotacja i backup bazy")
```
