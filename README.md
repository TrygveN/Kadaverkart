# Oppskrift på Kadaverkart
Her er en oppskrift på hvordan importere n50 kartdata i openorienteeringmapper for å lage et 1:50 000 kart med samme detaljer som turkart, men mer lesbart og egna for orienteringsløp.

Prosessesen er sånn som dette:

## 1. Last ned n50 data fra geonorge
https://kartkatalog.geonorge.no/metadata/n50-kartdata

Last ned n50 data i **POSTGIS format**.
Jeg prøvde forskjellige format, men fant ut at kun postgis ga meg alle kartdataen, gml mangla mye, og funka bra i qgis. Problemet er at du da må sette opp en postgres database med postgis utvidelser.

## 2. Sett opp en POSTGIS database og importer .sql fila
Dette er nok det vanskeligste steget og krever litt teknisk kompetanse. Dette må du google deg frem til

## 3. Koble qgis til postgis og last inn dataen
### Punkter
-  bygning_posisjon
- masttele
- tarn
- terrengpunkt


### Linjer
- bane
- hjelpekurve
- hoydekurve
- kystkontur
- luftledninglh
- lysloype
- skitrekk
- skytebaneinnretning
- veglenke


### Flater

- alpinbakke
- apentområde
- bygning_amrade
- bymessigbebyggelse
- dyrketmark
- elv
- golfbane
- gravplass
- havflate
- industriområde
- innsjo
- innsjoregulert
- myr
- park?
- skog
- sportidrettplass
- steinbrudd
- steintipp
- tettbebyggelse

## 4. Eksporter lagene fra qgis
OOM bruker samme biblioteket for vektordata som qgis så den støtter samme filtyper, men ikke alle er skapt like. Jeg bruker GPGK som skal være solid.

Her går det ann å ta et og et lag i hver sin fil, men det er enklere å samle de opp. Jeg strevde med å kombinere forskjellige geometrityper så det er lurt å ta en for flater, en for linjer og en for punkt.

"Package layers" i processsing toolbox funker bra til dette

Eneste untaket er at bygning_omrade bør være i en egen fil. Dette er fordi jeg ikke klarte å skille bygning punkt og flater .

## 4. Importer lagene i open orienteering mapper med crt fila
I OOM er en crt fil en krysstabell som kobler o-kart symbol med attributter i lagene.

For å laste inn lagene velger du `fil -> importer` og finner fila. Deretter får du opp "Tildel nye symboler". Her må du trykke `symboloversikt -> Åpne CRT-fil` og velge crt fila fra dette ropoet.
Det er en egen crt fil kun for bygning_omrade

## 5. Finpuss
Nå skal alt ligge i OOM. Nå er det bare å tilpasse farger og symboler etter smak og behag!

