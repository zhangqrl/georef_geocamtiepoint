var angle = 0;

/*
function saveImage() {
    $.post('',{'angle':angle})
        .success(function (data, status, jqXHR) {
            var json = data;
            $("#rotate_image").src = json.imageUrl;
        })
}
*/
function rotateLeftButtonClicked() {
    angle = angle-90; 
    document.getElementById('rotate_image').style['-webkit-transform'] = 'rotate('+angle+'deg)';

   $("#angle_value").val(angle); 
}    

function rotateRightButtonClicked() {
    angle = angle+90;
    document.getElementById('rotate_image').style['-webkit-transform'] = 'rotate('+angle+'deg)';
   $("#angle_value").val(angle); 
}