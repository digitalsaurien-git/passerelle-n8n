# Passerelle IA

Application personnelle de dialogue avec n8n, prête pour Docker et Coolify.

Fonctions :

- chat avec historique conservé sur le serveur ;
- choix OpenAI ou Claude à chaque message ;
- dictée vocale continue avec redémarrage automatique tant que l’utilisateur ne clique pas sur Arrêter ;
- ajout par bouton, glisser-déposer ou collage de PDF, images, TXT, MD, CSV, JSON, DOCX et XLSX ;
- extraction simple du texte des documents Word et Excel pour les deux fournisseurs ;
- reprise d’une conversation et ajout de compléments ;
- export texte et suppression ;
- mode démonstration local sans IA ;
- webhook protégé par un jeton envoyé uniquement par le serveur.

Commencez par ouvrir `DEMARRER-ICI.html`.

Les données persistantes se trouvent dans le volume Docker `passerelle-data`. L’application est prévue pour un usage personnel avec un seul mot de passe. Pour plusieurs utilisateurs, ajoutez une véritable gestion de comptes avant le déploiement.
