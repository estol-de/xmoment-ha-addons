# estol XMoment Connect

Verbindet Ihr **XMoment-Gerät** mit Home Assistant – ohne dass Sie Adressen oder Passwörter eintippen.
Danach erscheinen alle Casambi-Leuchten des XMoment mit genau ihren Funktionen (Helligkeit, Lichtfarbe,
Farbe …) und alle Szenen in Home Assistant.

## So geht's

1. Das offizielle **Mosquitto-Broker**-Add-on installieren und starten (falls noch nicht geschehen).
   Home Assistant bietet danach die **MQTT**-Integration an – bestätigen.
2. **estol XMoment Connect** installieren und starten.
3. Fertig. Das Add-on findet das XMoment im Heimnetz und verbindet es automatisch. Die Leuchten stehen
   nach wenigen Sekunden unter *Einstellungen → Geräte & Dienste → MQTT*.

Über **XMoment** in der Seitenleiste sehen Sie alle gefundenen Geräte, ihren Status und können sie
verbinden, trennen oder die Website des Geräts öffnen.

## Gut zu wissen

- Das XMoment muss im **selben Netz** wie Home Assistant sein.
- Das Add-on ist nur die Einrichtungshilfe. Die Leuchten laufen direkt zwischen XMoment und Home Assistant;
  wird das Add-on gestoppt, funktioniert alles weiter.
- Ein XMoment, das bereits an einem **anderen** MQTT-Broker hängt, wird nie automatisch übernommen –
  nur per Knopf „Zu Home Assistant umziehen“.
- Nach „Trennen“ verschwinden die Leuchten aus Home Assistant und das Gerät wird nicht wieder automatisch
  verbunden, bis Sie erneut auf „Verbinden“ drücken.
- Findet das Add-on das Gerät nicht (manche Router blockieren die automatische Suche), tragen Sie die
  Adresse des Geräts in der Seitenleiste oder unter *Konfiguration → Feste Geräte-Adressen* ein.
- Ändert sich das Mosquitto-Passwort, überträgt das Add-on es selbstständig neu.

## Hilfe

estol · www.estol.de
