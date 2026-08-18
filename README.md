# Bridge Satel Integra ↔ Tuya ↔ eBUS (RPi Zero W)

Szkielet działającej integracji między centralą Satel Integra 128 (moduł
ETHM-1 Plus), urządzeniami Tuya (lokalnie) oraz danymi z kotła Wolf (eBUS).

## Status tego kodu

To jest **działający szkielet**, nie gotowe "pod klucz" rozwiązanie -
kilka miejsc wymaga uzupełnienia danymi specyficznymi dla Twojej instalacji
(adresy IP, klucze, numery wejść) oraz jednej rzeczy do zweryfikowania:

- **Dokładny format URL dla "Wejść IP" ETHM-1 Plus** (`satel/virtual_inputs/url_template`
  w `config.example.yaml`) - skopiuj rzeczywisty adres/parametry z zakładki
  "Wejścia IP" w DLOADX po skonfigurowaniu konkretnego wejścia. Firmware może
  różnić się formatem zapytania między wersjami.
- **Wersja biblioteki `satel_integra`** (`bridge/satel_client.py`) - nasz
  kod jest napisany pod STARE, prywatne API pakietu (linia 0.3.x: konstruktor
  `AsyncSatel(host, port, loop, ...)`, `monitor_status()`, `keep_alive()`,
  `generate_query()`, atrybuty `_zone_changed_callback`/... ustawiane
  bezpośrednio). `requirements.txt` celowo przypina `<1.0.0` - pakiet
  `satel-integra` 1.0.0+ (wymaga Python 3.12+, więc na obrazie `python:3.11-slim`
  z Dockerfile w tym repo `pip` go i tak nie wybierze) to KOMPLETNIE
  PRZEPISANE API: inny konstruktor (bez `loop`, `integration_key` na
  starcie), `register_callbacks()`/`start(enable_monitoring=True)` zamiast
  `monitor_status()`, brak `generate_query()`. **To jest właśnie wersja,
  której obecnie używa integracja Satel w Home Assistant** (patrz
  `homeassistant/components/satel_integra/client.py`) - jej źródła są dobrym
  odniesieniem architektonicznym (patrz niżej), ale NIE nadają się jako
  bezpośredni przykład wywołań metod dla tego repo, bo to inne, niekompatybilne
  API. Jeśli kiedyś zajdzie potrzeba przejścia na 1.0.0+, to pełne
  przepisanie `satel_client.py`, nie drobna poprawka.
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

**296 testów, wszystkie przechodzą, pokrycie kodu ~76%.** Uczciwie o
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
3.9/3.11/3.12 przy każdym push/PR - 3.9 celowo, bo to najstarszy Python
realnie spotykany na Raspberry Pi OS (Bullseye).

## Instalacja na RPi Zero W

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Alternatywa bez instalowania Pythona/zależności ręcznie - patrz sekcja
"Uruchomienie w Dockerze" niżej.

## Konfiguracja

Dwie drogi - wybierz wygodniejszą:

### A) Kreator w przeglądarce (zalecane na start)

Jeśli `config.yaml` jeszcze nie istnieje, panel **automatycznie
przekierowuje** na `/setup` przy pierwszym wejściu - kreator **podzielony
na etapy** (przyciski Wstecz/Dalej, jeden logicznie spójny temat na
stronę), z przyciskami otwierającymi w nowej karcie strony, których
potrzebujesz (developer.tuya.com, ntfy.sh). Wystarczy:

```bash
python -m dashboard.app --config config.yaml
```

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
  osiągalności, NIE samego klucza integracji - protokół integracji Satela
  wysyła klucz dopiero przy konkretnym poleceniu (uzbrojenie/wyjście), a
  test świadomie niczego takiego nie wykonuje, żeby nic nie zmienić w
  systemie alarmowym. Błędny klucz integracji ujawni się dopiero przy
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
pliku roboczym `.setup_draft.json` obok `config.yaml`) - kreator da się
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

Te trzy rzeczy trafiają do osobnego pliku `automations.yaml`, nie do
`config.yaml` - i **zostają w pełni edytowalne PO zapisie**, z powrotem w
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
wartości domyślne w `config.yaml` - dopisujesz je później ręcznie (plik
zostaje w pełni edytowalny - kreator nie uruchomi się drugi raz, dopóki
plik istnieje, więc żadne Twoje ręczne zmiany nie zostaną nadpisane).

### B) Ręczna edycja plików

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
   sekcje "Automatyzacje" i "Panel webowy" niżej), jeśli chcesz mieć
   możliwość edycji tych rzeczy później z panelu (`/automations`, `/config`).
4. Local key urządzeń Tuya wyciągnij narzędziem:
   ```bash
   python -m tinytuya wizard
   ```

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

Poświadczenia wpisywane przy imporcie w kreatorze/panelu **nie są nigdzie
zapisywane** - używane jednorazowo do pobrania listy urządzeń, potem
zapominane (w przeciwieństwie do `tuya.cloud` w `config.yaml`, gdzie
zostają trwale, jeśli włączysz integrację kamer).

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
przycisk **"🔍 Rozpocznij nasłuch"** w zakładce ⚙ Ustawienia (`/config`) -
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

**A) Panel webowy** (`/config` - link "⚙ Ustawienia czujników eBUS" w
górnym pasku panelu głównego): tabela z listą czujników, gdzie każdy ma
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

## Rotacja i backup bazy

RPi Zero W ma ograniczoną kartę SD - `bridge_history.db` rosłaby w
nieskończoność bez czyszczenia (odczyty z kotła co kilkadziesiąt sekund,
zdarzenia ruchu, itd.). Domyślnie bridge **sam, automatycznie** czyści
dane starsze niż 90 dni raz na dobę (konfigurowalne, sekcja `maintenance`
w `config.yaml`) - zero dodatkowej konfiguracji potrzebnej "z pudełka".

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
0 3 * * * /home/pi/satel_tuya_bridge/venv/bin/python /home/pi/satel_tuya_bridge/maintenance.py --config /home/pi/satel_tuya_bridge/config.yaml >> /home/pi/satel_tuya_bridge/maintenance.log 2>&1
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
serwuje dane z SQLite przez lekki Flask + waitress.

Uruchomienie (osobny proces od głównego bridge'a):

```bash
python -m dashboard.app --config config.yaml --port 8090
```

Panel dostępny pod `http://<adres-rpi>:8090/`. Dwie zakładki dostępne z
górnego paska: **⚙ Ustawienia** (`/config` - czujniki eBUS, pola Satela,
urządzenia Tuya, patrz niżej) i **🎬 Automatyzacje** (`/automations` -
sceny/reguły ruchu/harmonogramy/eskalacja/symulacja obecności, patrz
sekcja "Automatyzacje" niżej) - obie odczytują bez hasła, zapis wymaga
hasła administratora.

### Wykres historii - wybór wielu czujników naraz

Przycisk **"Czujniki (N) ▾"** nad wykresem otwiera panel z listą WSZYSTKICH
czujników, które mają choć jeden zapisany odczyt (tylko liczbowe czujniki
eBUS - Satel/Tuya mają na razie tylko status na żywo, bez historii do
wykresu). Lista jest:

- **pogrupowana wg obwodu** - ten sam mechanizm co grupowanie w `/config`
  (drugi segment tematu MQTT, np. `ebusd/bm/...` i `ebusd/fha/...` trafiają
  do osobnych grup "bm"/"fha"),
- **filtrowalna** - pole tekstowe nad listą filtruje po nazwie/etykiecie,
- **wielokrotnego wyboru** - zaznacz kilka czujników naraz, żeby nałożyć
  je na jednym wykresie (np. porównać temperaturę zasilania i powrotu) -
  każdy dostaje inny kolor linii, z osobną legendą.

Wybór zapamiętywany jest tylko w pamięci karty przeglądarki (jak reszta
stanu UI) - znika przy odświeżeniu strony, przy starcie domyślnie
zaznaczony jest pierwszy czujnik na liście.

### Zakładka ⚙ Ustawienia - eBUS, Satel, Tuya bez edycji YAML-a

Trzy sekcje na jednej stronie (`/config`), każda z własnym przyciskiem
zapisu (osobny plik na sekcję - patrz "Automatyzacje" niżej po wyjaśnienie
tego wzorca):

- **Czujniki eBUS** (`sensors.yaml`) - jak dotychczas, plus przycisk
  **"🔍 Wykryj czujniki"**: nasłuchuje ok. 4 sekundy na skonfigurowanym
  brokerze MQTT (`ebus.mqtt_host`) i dopisuje wiersz dla każdego tematu, na
  który `ebusd` w tym czasie coś opublikował - **to podpowiedź, nie pełna
  lista pól** (rzadziej publikowane pola mogą się nie pojawić za pierwszym
  razem - kliknij ponownie albo dodaj ręcznie, patrz "Jak znaleźć listę
  dostępnych pól" niżej).
- **Satel - strefy/wyjścia/wejścia IP/partycje** (`satel_fields.yaml`) -
  przycisk **"🔍 Wykryj aktywne strefy/wyjścia"** (jak w kreatorze): protokół
  integracji Satela nie udostępnia listy WSZYSTKICH skonfigurowanych
  stref/wyjść wraz z nazwami (ograniczenie protokołu, nie da się tego
  obejść), więc przycisk nasłuchuje 30 sekund i notuje numery, które w tym
  czasie FIZYCZNIE zmienią stan (otwórz/zamknij drzwi i okna, wywołaj
  czujki ruchu, załącz wyjście) - dodaje je jako nowe wiersze niżej, nadaj
  im nazwę i zapisz. Strefy, które nie zmienią stanu w tym oknie, trzeba
  dodać ręcznie (numer z DLOADX) - host/port centrali brane są automatycznie
  z zapisanego już `config.yaml` (nie trzeba ich przepisywać z zakładki
  "Połączenia"). Każda strefa/wyjście ma dodatkowo **opcjonalny "rodzaj"**
  (np. drzwi/okno/ruch/dym/zalanie/wibracja/sabotaż dla wejść, syrena/
  przekaźnik/oświetlenie/brama/ogrzewanie dla wyjść) - czysto opisowy,
  dokłada tylko ikonę i etykietę rodzaju na panelu głównym (zielone
  OK/czerwone naruszone zostaje bez zmian niezależnie od wybranego rodzaju).
- **Urządzenia Tuya** (`tuya_devices.yaml`) - przycisk
  **"🔍 Skanuj sieć"** wywołuje broadcast UDP w LAN (`tinytuya.deviceScan`,
  ok. 18 sekund) i podpowiada ID + adres IP znalezionych urządzeń. **Nie
  podpowiada `local_key`** - Tuya świadomie go nie rozgłasza, trzeba go
  wyciągnąć osobno (`python -m tinytuya wizard`). Sekcja **"🐝 Bramki
  Zigbee"** w tym samym panelu pozwala dodać czujniki/przełączniki Zigbee
  podpięte pod bramkę Tuya (np. Tuya Zigbee Gateway/Hub) - patrz "Urządzenia
  Zigbee przez bramkę Tuya" niżej. Każde skonfigurowane urządzenie od razu
  dostaje kartę na stronie głównej (nie dopiero po pierwszym udanym
  odpytaniu) - jeśli akurat nie odpowiada (zły `local_key`/IP/wersja
  protokołu, urządzenie offline), karta zostaje widoczna z wartością
  "niedostępne" zamiast całkiem zniknąć, więc od razu widać, że urządzenie
  JEST skonfigurowane, tylko coś jest z nim nie tak.

Każde pole zaczyna jako **nieaktywne** (nie ma go w konfiguracji), dopóki
nie uzupełnisz wiersza (po odkryciu albo ręcznie) i nie klikniesz "Zapisz"
dla danej sekcji - odkrycie samo w sobie niczego nie włącza ani nie
zapisuje.

Zakładka **🔌 Połączenia** ma dodatkowo przycisk **"🔄 Zrestartuj bridge i
panel teraz"** - w odróżnieniu od reszty tej strony (patrz wyżej: zmiany
wchodzą w życie automatycznie bez restartu), zmiany host/port/kluczy/
brokerów MQTT w TEJ zakładce wymagają pełnego restartu procesu (żywe
połączenia TCP nie są odtwarzane w locie). Przycisk restartuje OBA
kontenery Dockera (`docker-compose.yml` ma dla obu `restart: unless-stopped`
- proces po prostu kończy się, Docker sam go podnosi ponownie) - bez
potrzeby dostępu do konsoli/SSH. Uruchomienie bez Dockera (`python main.py`
w tle/systemd) NIE ma tego automatycznego podniesienia - proces po prostu
się zakończy, trzeba go wystartować ręcznie/przez systemd.

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

**Jak dodać (zakładka ⚙ Ustawienia → Urządzenia Tuya):**

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

Zapis czujników eBUS, pól Satela, urządzeń Tuya (ta strona, `/config`) albo
scen/reguł ruchu/symulacji obecności (`/automations`) **nie wymaga
restartu** bridge'a ani całego kontenera Docker - panel prosi bridge o
przeładowanie konfiguracji w tle (przez tę samą kolejkę komend co
uzbrojenie/rozbrojenie, patrz "Jak działa uzbrajanie/rozbrajanie
technicznie" niżej), a bridge podłapuje zmianę w ciągu ok. 1-2s. Widać to
w komunikacie po zapisie oraz jako zdarzenie "Konfiguracja przeładowana"
w dzienniku zdarzeń panelu głównego.

**Wyjątek: zakładka 🔌 Połączenia** (host/port centrali Satel, adresy
brokerów MQTT, poświadczenia Tuya Cloud, Home Assistant) - te ustawienia
dotyczą ŻYWYCH połączeń sieciowych (gniazdo TCP do centrali, klienci
MQTT, Tuya Cloud Pulsar), więc bezpieczne zamknięcie i odtworzenie ich w
locie byłoby dużo bardziej ryzykowne niż warte tego dla ustawień, które i
tak zmienia się rzadko. Zmiana czegokolwiek w tej zakładce **wciąż
wymaga pełnego restartu** procesu bridge'a - patrz "Build i rebuild -
krok po kroku" (Docker) albo `sudo systemctl restart satel-tuya-bridge`
(systemd).

### Zakładka 🔌 Połączenia - host/port/klucze/tematy MQTT po pierwszej konfiguracji

Kreator (`/setup`) jest **celowo zablokowany** po pierwszym zapisie (żeby
przypadkowe ponowne wejście na `/setup` nigdy nie nadpisało ręcznych zmian
w `config.yaml`) - więc host/port centrali Satel, klucz integracji, adres
brokera MQTT, poświadczenia Tuya Cloud, Home Assistant i tematy MQTT
zmienia się **z panelu**, w zakładce **🔌 Połączenia** (`/config`, sekcja
na górze strony). W odróżnieniu od reszty `/config` - **odczyt też wymaga
hasła administratora** (kliknij "🔒 Wczytaj bieżące ustawienia"), bo dane
obejmują klucz integracji Satela i sekrety Tuya Cloud; formularz nigdy nie
pokazuje prawdziwej wartości sekretu z powrotem - tylko informację, czy
jest już ustawiony. Zostaw pole hasła/klucza puste, żeby go NIE zmieniać.

Zmiany z tej zakładki dotykają **tylko** pól połączeń w `config.yaml` -
reszta pliku (reguły kamer/alarmów, automatyka kotła, ustawienia
konserwacji) zostaje nietknięta.

### Zakładka 🧩 Wtyczki - jeden przegląd wszystkich integracji

`/plugins` zbiera WSZYSTKIE integracje (Satel, Tuya lokalnie/Zigbee/Cloud,
eBUS, Home Assistant, publikację stanu do MQTT, UniFi Protect, symulację
obecności, powiadomienia push) w jednym czytelnym miejscu - każda jako
karta z ikoną, opisem, stanem (Włączona/Wyłączona/Nieskonfigurowana) i
liczbą skonfigurowanych elementów (np. "3 urządzenia").

To **przegląd i przełącznik, nie edytor** - część integracji (Satel, Tuya
lokalnie/Zigbee, eBUS, UniFi Protect, powiadomienia) jest włączona/
wyłączona samą OBECNOŚCIĄ konfiguracji (np. dodanie choć jednego
urządzenia Tuya) i pokazuje się tu tylko do odczytu, z linkiem do
właściwej zakładki. Cztery integracje mają osobną, zapisaną flagę
`enabled`, którą da się przełączyć bezpośrednio z tego ekranu (bez
przechodzenia do innej zakładki): **Tuya Cloud**, **Home Assistant**,
**publikacja stanu do MQTT** i **symulacja obecności** - ale WŁĄCZENIE
integracji, która nigdy wcześniej nie miała podanych wymaganych danych
(np. Tuya Cloud bez zapisanego Access ID), nadal wymaga najpierw
uzupełnienia ich we właściwej zakładce (przełącznik odrzuci taką próbę z
czytelnym błędem).

Trzy z czterech przełączalnych integracji (Tuya Cloud, Home Assistant,
publikacja MQTT) żyją w `config.yaml` i **wymagają pełnego restartu**
procesu bridge'a, tak samo jak zmiany w zakładce 🔌 Połączenia (widać to
jako odznakę "wymaga restartu" na karcie). Symulacja obecności jest
wyjątkiem - żyje w `automations.yaml` i włącza/wyłącza się na żywo w
ciągu ok. 1-2s, bez restartu (patrz "Zmiany działają od razu" wyżej).

**Interwał odpytywania (Satel i Tuya lokalnie).** Te dwie karty mają
dodatkowo pole "Interwał odpytywania" - jak często bridge SAM dopytuje o
bieżący stan wejść/wyjść (Satel: `satel.status_poll_interval_seconds`,
domyślnie 15s) i urządzeń Tuya (`tuya.status_poll_interval_seconds`,
domyślnie 30s, obejmuje też podurządzenia Zigbee - ta sama pętla). To
osobny mechanizm od broadcastów samej centrali/urządzeń o zmianie stanu -
zaobserwowano na realnej instalacji, że zmiana stanu WYJŚCIA nie zawsze
dociera jako broadcast (w odróżnieniu od wejść), więc samo poleganie na
"centrala/urządzenie samo powie" nie wystarcza do rzetelnie ciągłej
synchronizacji panelu/MQTT. W odróżnieniu od włączania/wyłączania
integracji - **to zawsze działa na żywo, bez restartu** (minimum 3s, żeby
nie zalewać centrali/urządzeń zapytaniami przy pomyłce we wpisanej
wartości).

### Publikacja stanu do MQTT (niezależnie od Home Assistant)

Stan wejść/wyjść/stref Satela i urządzeń Tuya jest publikowany na
skonfigurowany broker MQTT pod osobnym tematem per integracja -
**niezależnie od tego, czy Home Assistant jest włączony** (dotychczas
publikacja bez HA działała tylko dla danych z kotła eBUS, przez
`boiler_output.mqtt.topic_prefix`). Domyślne tematy: `satel/...` i
`tuya/...`, konfigurowalne w zakładce 🔌 Połączenia (sekcja "Publikacja
stanu do MQTT") albo wprost w `config.yaml`:

```yaml
mqtt_publish:
  enabled: true      # domyślnie true, jeśli ebus.mqtt_host jest ustawiony
  topics:
    satel: satel      # -> satel/input/<numer>/state, satel/output/<numer>/state, satel/partition/<numer>/state
    tuya: tuya         # -> tuya/<nazwa_urządzenia>/state
```

Temat MQTT dla danych z kotła eBUS (dotychczasowy `boiler_output.mqtt.
topic_prefix`, domyślnie `home/boiler`) jest teraz też edytowalny z tej
samej zakładki, jako "Temat MQTT dla stanu eBUS".

### Alarmy progowe

Reguły w `config.yaml` -> `alarm_rules` pozwalają zdefiniować progi (np.
temperatura zasilania kotła powyżej 85°C) oraz wykrywanie braku danych
(np. moduł eBUS przestał odpowiadać przez 10 minut). Aktywny alarm pojawia
się jako czerwony baner w panelu i - jeśli skonfigurujesz
`notifications.ntfy_topic_url` - jako powiadomienie push przez
[ntfy.sh](https://ntfy.sh) (darmowe, można też self-hostować).

### Bezpieczeństwo panelu - autoryzacja tylko dla akcji zmieniających stan

Panel jest **celowo** podzielony na dwie strefy:

- **Otwarte bez hasła**: cały podgląd - historia, statusy urządzeń,
  dziennik zdarzeń, aktywne alarmy. To świadomy wybór, nie przeoczenie -
  w domowej sieci LAN podgląd stanu domu nie jest tak wrażliwy jak
  możliwość jego ZMIANY.
- **Wymaga zalogowania**: zapis ustawień czujników (`/config`,
  `/automations`) oraz uzbrajanie/rozbrajanie stref alarmowych z panelu
  głównego.

#### Konfiguracja konta administratora (nazwa użytkownika + hasło)

Ustawiane normalnie RAZ w kreatorze (`/setup`, krok "Hasło administratora
panelu") - nazwa użytkownika (domyślnie `admin`, dowolna) i hasło (min. 8
znaków). Ręcznie (bez kreatora, np. edytując `config.yaml` wprost):

```bash
python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('TWOJE_HASLO'))"
```

Wynik wklej do `config.yaml` -> `dashboard.auth.password_hash`, nazwę
użytkownika do `dashboard.auth.username` (jawnym tekstem - to NIE jest
sekret, tylko identyfikator, jak login, nie hasło). Dopóki `password_hash`
jest puste, akcje zmieniające stan są **zablokowane** (nie ma trybu
"otwartego na wszystko" przez pomyłkę - brak hasła = brak zapisu).
`username` bez ustawionej wartości domyślnie to `admin` (dotyczy też
configów sprzed dodania tego pola - aktualizacja pakietu nikogo nie
wylogowuje/blokuje).

**Uczciwie o obecnym braku**: zmiana nazwy użytkownika/hasła PO pierwszej
konfiguracji wymaga ręcznej edycji `config.yaml` (wygeneruj nowy hash jak
wyżej) i restartu panelu - w przeciwieństwie do reszty ustawień (Satel/
Tuya/eBUS), zakładka 🔌 Połączenia świadomie NIE oferuje do tego formularza
(zmiana własnego hasła administratora bez potwierdzenia bieżącego hasła to
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
sieci) - wpisujesz dane logowania RAZ, serwer wystawia losowy token sesji w
ciasteczku **`HttpOnly`** (niedostępnym dla JavaScriptu na stronie -
ochrona przed odczytaniem go przez ewentualny złośliwy skrypt/XSS).
Dalsze żądania z panelu **nie wysyłają już hasła w ogóle** - tylko ten
token, aż do wylogowania (link "🔓 Wyloguj" w nawigacji każdej strony,
wywołuje `POST /api/logout`) albo wygaśnięcia sesji (12h).

Próba akcji wymagającej logowania bez ważnej sesji (np. wejście prosto na
`/config` i kliknięcie "Zapisz") przekierowuje na `/login?next=...` -
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

**Nie.** `satel.integration_key` w `config.yaml` (ten sam, który wpisujesz
w kreatorze) to **hasło protokołu integracji** - autoryzuje polecenia
(uzbrojenie/rozbrojenie, sterowanie wyjściem), ale **nie jest kluczem
szyfrowania transmisji**. Biblioteka `satel_integra` w wersji, na której
oparty jest ten bridge (0.3.x - patrz wyjaśnienie w `requirements.txt`
i sekcji "Status tego kodu" wyżej, dlaczego jest to celowo przypięte),
**nie implementuje** opcjonalnego szyfrowania AES komunikacji TCP, jakie
oferuje protokół integracji części central/modułów ETHM (ustawianego
osobno w DLOADX, innym kluczem niż hasło integracji) - cała komunikacja
z centralą (port 7094) i wywołania wirtualnych wejść IP (port z sekcji
`virtual_inputs`) idą **czystym tekstem** po sieci LAN. UWAGA: nowsza
wersja tej biblioteki (1.0.0+, obecnie używana przez integrację Satela w
Home Assistant) OBSŁUGUJE już to szyfrowanie - ale to inne, niekompatybilne
API (patrz wyżej), więc przejście na nią to pełne przepisanie
`satel_client.py`, nie prosta aktualizacja pakietu.

W praktyce, w zaufanej, odizolowanej sieci domowej to zwykle akceptowalne
ryzyko (dokładnie ta sama klasa kompromisu co Basic Auth panelu wyżej).
Jeśli jednak centrala/RPi są w sieci współdzielonej z niezaufanymi
urządzeniami, albo zależy Ci na dodatkowej warstwie ochrony:
- odizoluj VLAN-em urządzenia automatyki domowej od reszty sieci,
- **nie** wystawiaj portu centrali (7094) ani portu wejść IP na świat -
  jeśli potrzebujesz zdalnego dostępu, użyj VPN (WireGuard/Tailscale) do
  całej podsieci, a nie przekierowania portu,
- rozważ zgłoszenie/wsparcie szyfrowania w `satel_integra` upstream, jeśli
  Twoja centrala i tak wymaga go po stronie DLOADX.

#### A uzbrajam/rozbrajam z panelu, nic się nie dzieje na centrali (ale stan wyświetla się poprawnie)

Do wersji z tej rundy poprawek `arm()`/`disarm()`/`set_output()` w
`satel_client.py` uznawały samo WYSŁANIE bajtów po sieci za sukces -
biblioteka `satel_integra` NIE czeka sama na potwierdzenie z centrali
(ramka 0xEF "command result"). Błędny `satel.integration_key` albo
odrzucenie komendy przez centralę kończyło się więc CISZĄ porażką: w
logach/panelu komenda wyglądała na wykonaną, a fizycznie nic się nie
działo - podczas gdy ODCZYT stanu (broadcast/cykliczny poll) działał
poprawnie, bo w ogóle nie przechodzi przez to potwierdzenie. Teraz
`_send_and_confirm()` w `satel_client.py` czeka na odpowiedź centrali i
rzuca czytelny błąd (widoczny w statusie komendy w panelu), jeśli centrala
odrzuci polecenie albo nie odpowie w 5s - jeśli dalej nic się nie dzieje,
sprawdź w logach dokładny komunikat błędu.

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

## Automatyzacje: sceny, harmonogramy, eskalacja, symulacja obecności

Oprócz prostych reguł "ruch -> włącz urządzenie", bridge wspiera bardziej
złożoną automatykę - wszystko zbudowane na jednym wspólnym fundamencie:
**scenach** (`bridge/scenes.py`).

**W pełni konfigurowalne z przeglądarki, bez dotykania YAML-a** - zakładka
**🎬 Automatyzacje** (`/automations`, link z górnego paska panelu) pozwala
dodawać/edytować/usuwać sceny, reguły ruchu (harmonogram + eskalacja) i
symulację obecności. Odczyt jest otwarty, **zapis wymaga hasła
administratora panelu** (tego samego z kroku "Hasło administratora panelu"
w kreatorze - patrz sekcja "Bezpieczeństwo panelu" niżej). Alternatywnie:
te same trzy rzeczy da się skonfigurować już **w kreatorze** przy
pierwszym uruchomieniu (kroki "Sceny" i "Reguły ruchu"), albo ręcznie w
pliku `automations.yaml` (patrz niżej) - wszystkie trzy drogi prowadzą do
tego samego pliku.

### Gdzie to jest zapisane: `automations.yaml`, nie `config.yaml`

Sceny/reguły ruchu/symulacja obecności trafiają do **osobnego pliku**
`automations.yaml` (obok `config.yaml`, ścieżka konfigurowalna polem
`automations_file` w `config.yaml`) - z dokładnie tego samego powodu, dla
którego czujniki eBUS mają swój `sensors.yaml`: to jedyne pliki, które
UI (kreator i panel `/automations`) wolno mu bezpiecznie **nadpisać w
całości** przy każdym zapisie, bez ryzyka skasowania Twoich ręcznych
zmian/komentarzy w `config.yaml`. Format pliku - patrz
`config.example.yaml` (sekcje `scenes`/`reguły_ruchu`/
`presence_simulation`) i przykłady niżej.

Ten sam wzorzec (osobny plik, konfigurowalna ścieżka, panel może go
bezpiecznie nadpisać w całości) obowiązuje też dla pól edytowanych w
zakładce ⚙ Ustawienia (patrz sekcja "Panel webowy" wyżej):
`satel_fields.yaml` (`satel.fields_file`) dla stref/wyjść/wejść IP/
partycji, `tuya_devices.yaml` (`tuya.devices_file`) dla urządzeń Tuya.
Wszystkie cztery pliki są opcjonalne - jeśli nie istnieją, wartości brane
są wprost z `config.yaml` (wsteczna zgodność).

Configi sprzed tego podziału (gdzie te sekcje były wpisane bezpośrednio w
`config.yaml`) wciąż działają bez zmian - `automations.yaml` jest
całkowicie opcjonalny; jeśli nie istnieje, bridge czyta te sekcje wprost z
`config.yaml` jak dawniej. Pierwszy zapis z panelu `/automations` tworzy
plik i **od tej pory to on jest źródłem prawdy** dla tych trzech sekcji
(wartości w `config.yaml`, jeśli tam nadal są, są wtedy ignorowane).

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
długość geograficzna) w `config.yaml`. Okna przechodzące przez północ
(np. `22:00`-`06:00`) działają poprawnie - sprawdzone testem na kilku
przypadkach brzegowych.

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

## Uruchomienie jako usługa systemd

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
`satel-tuya-dashboard.service`, `ExecStart=... python -m dashboard.app --config config.yaml --port 8090`).

## Uruchomienie w Dockerze

Alternatywa dla instalacji przez `venv`/systemd wyżej - jeden obraz
(`Dockerfile`), dwa kontenery z `docker-compose.yml` (bridge + panel
webowy), dzielące wspólny wolumen na dane (`config.yaml`, `sensors.yaml`,
baza SQLite, backupy). Działa tak samo na RPi (arm64/armv7) jak na
amd64 - `python:3.11-slim` ma gotowe obrazy dla obu architektur.

### Pierwsze uruchomienie

```bash
mkdir -p data
sudo chown 1000:1000 data   # kontener działa jako UID 1000 (nie root)

docker compose up -d --build
```

Otwórz `http://<adres-hosta>:8090/setup` w przeglądarce i przejdź kreator
dokładnie tak samo, jak przy uruchomieniu bez Dockera - `config.yaml`,
`sensors.yaml` i baza SQLite wylądują w `./data/` na hoście (a nie w
kontenerze), więc przetrwają przebudowę/aktualizację obrazu i są dostępne
do ręcznej edycji z hosta. Po zapisaniu w kreatorze kontener `bridge`
trzeba zrestartować, żeby wczytał świeżo zapisaną konfigurację:

```bash
docker compose restart bridge
```

### Build i rebuild - krok po kroku

Wszystkie polecenia niżej uruchamiasz z katalogu repo (tam, gdzie jest
`docker-compose.yml`). Rozróżnienie, które ma znaczenie: **zmiana w
`config.yaml`/`sensors.yaml`/itd. (przez panel albo ręcznie) wymaga tylko
restartu** (dane są w zamontowanym `./data/`, nie w obrazie) - **zmiana w
kodzie (`bridge/`, `dashboard/`, `requirements.txt`, `Dockerfile`) wymaga
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

**3. Restart BEZ rebuildu (wystarczy po zmianie tylko w `config.yaml`/
`sensors.yaml`/`automations.yaml`/`satel_fields.yaml`/`tuya_devices.yaml`,
np. przez panel `/config` albo `/automations` - te pliki żyją w `./data/`,
nie w obrazie, więc rebuild niczego by tu nie zmienił):**
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
  (RPi) albo osobnym urządzeniu; w `config.yaml` (`ebus.mqtt_host`)
  wskaż adres LAN tego, co faktycznie działa. Włączenie:
  ```bash
  docker compose --profile broker up -d
  ```

### Sieć LAN (Satel, Tuya)

Domyślnie kontenery używają zwykłej sieci mostkowanej Dockera -
połączenia WYCHODZĄCE do centrali Satel i urządzeń Tuya (skonfigurowanych
po adresie IP w `config.yaml`) działają bez dodatkowej konfiguracji. Jeśli
coś w Twojej sieci tego wymaga (np. discovery po multicaście), w
`docker-compose.yml` przy usłudze `bridge` odkomentuj `network_mode: host`
(tylko Linux).

### Bezpieczeństwo obrazu

Kontener działa jako **użytkownik bez uprawnień roota** (UID 1000) -
stąd wymóg `chown` katalogu `data/` przed pierwszym uruchomieniem
(inaczej Docker utworzy go jako root i kontener nie będzie mógł zapisać
`config.yaml`). Sekrety (klucz integracji Satela, local_key urządzeń
Tuya, hasła) trafiają wyłącznie do zamontowanego `./data/config.yaml` -
nigdy do obrazu ani do warstw builda.

## Struktura projektu

```
satel_tuya_bridge/
  config.example.yaml       # wzór głównej konfiguracji (połączenia, poświadczenia)
  sensors.example.yaml      # wzór listy czujników eBUS (edytowalny z panelu, patrz /config)
  # automations.yaml (sceny/reguły ruchu/symulacja obecności), satel_fields.yaml
  # (strefy/wyjścia/wejścia IP/partycje) i tuya_devices.yaml (urządzenia Tuya)
  # NIE MAJĄ wzoru - tworzy je kreator albo pierwszy zapis z panelu
  # (/automations, /config); format pól - patrz odpowiednie sekcje w
  # config.example.yaml (scenes/reguły_ruchu/presence_simulation/satel/tuya)
  requirements.txt
  Dockerfile                # jeden obraz dla bridge'a i panelu, patrz "Uruchomienie w Dockerze"
  docker-compose.yml         # usługi bridge/dashboard/(opcjonalnie) mosquitto
  .dockerignore
  docker/mosquitto.conf      # minimalny config opcjonalnego brokera MQTT (profil "broker")
  main.py                   # punkt wejścia głównego bridge'a
  bridge/
    config.py               # loader YAML
    satel_client.py         # protokół integracji + wirtualne wejścia IP + test_connection() ("Testuj połączenie")
    tuya_local.py            # sterowanie lokalne (tinytuya) + scan_network()/fetch_cloud_devices() (import w /config)
    tuya_cloud_events.py     # ruch z kamer Tuya (Pulsar, wymaga chmury)
    unifi_protect_events.py  # ruch z kamer UniFi Protect
    ebus_client.py           # odczyt danych z ebusd (MQTT) + discover_topics() (podpowiedź do /config)
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
    rules_engine.py          # spina wszystko razem wg config.yaml
  dashboard/
    app.py                   # backend panelu (Flask + waitress)
    auth.py                   # autoryzacja - tylko akcje zmieniające stan
    setup.py                   # logika kreatora + budowanie config.yaml i towarzyszy
    templates/index.html     # frontend panelu (Chart.js, filtrowanie, sterowanie alarmem)
    templates/config.html    # edytor czujników eBUS / pól Satela / urządzeń Tuya (+ przyciski wykrywania)
    templates/automations.html # edytor scen/reguł ruchu/symulacji obecności (wymaga hasła do zapisu)
    templates/setup.html      # kreator pierwszej konfiguracji (krok po kroku, autozapis postępu)
  tests/                     # testy pytest (patrz sekcja "Testy automatyczne")
  .github/workflows/tests.yml  # CI - uruchamia testy na Python 3.9/3.11/3.12
  maintenance.py              # CLI do rotacji/backupu bazy (do crona, patrz sekcja "Rotacja i backup bazy")
```
