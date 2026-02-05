# Bayawan City Map Setup

To complete the interactive map setup:

1. **Export your map from Canva**
   - Export as high-resolution PNG or JPG
   - Save it as `bayawan_map.png` in the `static/images/` directory

2. **Configure the map coordinates**
   - Open `guest_app/templates/map.html`
   - Adjust the `bounds` variable based on your image dimensions
   - For a 1200×900 image, try: `var bounds = [[0, 0], [900, 1200]];`

3. **Add real points of interest**
   - Edit the `locations` array in the JavaScript code
   - For each location, provide:
     - `name`: The name of the location
     - `lat` and `lng`: The X and Y coordinates on your image
   - Example:
     ```javascript
     var locations = [
       { name: "City Plaza", lat: 500, lng: 550 },
       { name: "Bus Terminal", lat: 450, lng: 400 },
       // Add more locations...
     ];
     ```

4. **Test your map**
   - Open the map page in your browser
   - Check that:
     - The map displays correctly
     - Markers are positioned properly
     - Search functionality works

5. **Customize the appearance**
   - Adjust the CSS in the `<style>` section to match your site's design
   - You can customize marker icons, popup styles, and more

## Tips for Finding Coordinates

1. Add a click event listener to help find coordinates:
   ```javascript
   map.on('click', function(e) {
     console.log(e.latlng);
     alert("Coordinates: " + e.latlng.lat + ", " + e.latlng.lng);
   });
   ```

2. Add this code temporarily to help position your markers, then remove it once done.

## Benefits of This Approach

- No Google Maps API key required
- No API usage limits or costs
- Complete control over the appearance
- Faster loading times
- Works offline without internet connection 